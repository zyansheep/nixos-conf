#!/usr/bin/env python3
"""Time to empty / time to full with intervals, for the Waybar battery outline.

Discharging. Remaining energy is ∫ V(q) dq over the remaining charge, with the
loaded-voltage curve V(q) learned from the power log (each percent of this
gauge's coulomb-counted charge holds ~15% less energy near empty than near full),
and the battery's current reading calibrated against its charge counter. Future
average draw over the horizon is forecast as an Ornstein–Uhlenbeck process in
log watts, fitted to minute-level battery power: the recent draw decays toward
the local level (the mean log draw of the last ~5 hours on battery, not a
30-day mean that lags behind changes in how the laptop is used) with a learned
time constant, and the variance of the horizon average (plus uncertainty in
the level) gives a log-normal interval. Backtested day by day against the
30-day watts version: ~0.5 W closer over the next hour, on a par with the
Chronos-2 forecasting model. The horizon is solved jointly with the answer
(T = energy / mean draw over T).

Empty is where the embedded controller cuts the power, not zero charge: it
switches the laptop off with ~55 mAh still on the gauge (1.8% of full), so
energy counts down to that cutoff, learned from every battery death
(fit_cutoff). In the last FINAL_MINUTES the gauge's own falling charge,
extrapolated to the cutoff, gives the second it cuts out (backtested on five
deaths, leaving each out: within ~10 s on average, 35 s at worst, from two
minutes before). Below LOW_BATTERY a black box fsyncs the last reading every
2 s (lastgasp.json), so the next boot learns exactly when it died. Once a
death has taught the cutoff, battery-eta powers the laptop off cleanly
POWEROFF_SECONDS before it (and counts down to that), recording the
power-off so it isn't taken for a death.

Charging. The label shows the time on battery banked so far (current energy
at the typical battery draw) and the time to the charge limit, both marked ~
(their 80% ranges, ≈±20% and ≈±30%, are in the menu). The menu also shows
minutes of battery use bought per minute of charging: energy entering the
battery over the forecast average battery draw. Time to the charge limit (for the menu) follows the charge curve learned
from past charging (constant current, then taper), scaled by the current rate;
its interval comes from how far 10-minute rates stray from that curve.

Training. `battery-eta` is the single trainer: at start and every 15 minutes it
fits these models and the battery-use attribution models (report.fit) on the
last 30 days and writes them all to the power log's models.json, which the
collector's menu breakdown and the battery panel read.

`battery-eta` prints one Waybar JSON line every 2 s (time, ± half the 80%
interval, then watts) and mirrors the estimate to
$XDG_RUNTIME_DIR/waybar-monitor/eta.json for the battery panel.
"""
import collections
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

# Before numpy: these are small matrices, and BLAS threads only contend for them
# (a 226×76 least squares took 27 ms with 12 threads, 4 ms with one).
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

import numpy as np  # noqa: E402

import power  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from power import find_battery  # noqa: E402

Z80 = 1.2816  # 80% interval: 10th–90th percentile.
REFIT = 15 * 60
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
LEVEL_MINUTES = 300  # The local level: the last ~5 hours on battery (the best window in backtests of 100–1000).
DEFAULTS = {'mean': 15.0, 'level': math.log(15.0), 'level_n': 60, 'var': 0.08, 'tau': 20.0, 'kappa': 1.0, 'volts': None,
            'charge_curve': None, 'charge_sigma': 0.25, 'floor': 4.0,
            'cutoff': 0.0, 'cutoff_spread': 0.003, 'deaths': 0}  # Ah: until a battery death says otherwise.
CUTOFF_PCT = 3  # A boot that ended on battery below this percent was cut off (all five so far at 2%).
FINAL_MINUTES = 3  # From here the gauge's own trend times the cutoff.
COUNTDOWN_MINUTES = 10  # Below this, the label counts minutes and seconds.
LOW_BATTERY = 5  # Percent: below it the black box records every reading.
WARN_SECONDS = (300, 60)  # Notifications this long before the cutoff.
POWEROFF_SECONDS = 30  # A clean power-off this long before the cutoff, once a death has taught it.
GASP = store.STATE / 'lastgasp.json'  # The black box: the last reading, fsynced.
DEATHS = store.STATE / 'deaths.json'  # Every battery death its black box timed.


# --- Fitting ----------------------------------------------------------------------

def runs(rows):
    """Split minute rows into contiguous runs (no gap over a minute)."""
    current = []
    for m in rows:
        if current and m['t'] - current[-1]['t'] > 60:
            yield current
            current = []
        current.append(m)
    if current:
        yield current


def fit_ou(rows, max_lag=60):
    """Local level, variance and autocorrelation time (minutes) of log battery power.

    `rows` are battery minutes in time order. Each minute's deviation from the
    mean log draw of the LEVEL_MINUTES before it (once there are 30) gives the
    variance, and its autocorrelation within runs the time constant. Draw reverts
    evenly in log watts (a 20 W stretch falls ~3 W, a 10 W one rises ~1 W).
    """
    allx = np.array([m['bat'] for m in rows])
    if len(allx) < 30:
        return None
    z = np.log(np.maximum(allx, 0.1))
    total = np.concatenate([[0.0], np.cumsum(z)])
    x, start = [], 0
    for run in runs(rows):
        i = np.arange(start, start + len(run))
        start += len(run)
        first = np.maximum(0, i - LEVEL_MINUTES)
        known = i - first >= 30
        if known.any():
            x.append((z[i] - (total[i] - total[first]) / np.maximum(i - first, 1))[known])
    level = z[-LEVEL_MINUTES:]
    fitted = {'mean': float(allx.mean()), 'level': float(level.mean()), 'level_n': len(level),
              'floor': float(np.percentile(allx, 2))}
    if sum(len(r) for r in x) < 30:
        return fitted  # Too little history for deviations: the default variance and time constant.
    var = float(np.mean(np.concatenate(x) ** 2))
    lags, rhos = [], []
    for k in range(1, max_lag + 1):
        a = np.concatenate([r[:-k] for r in x if len(r) > k] or [np.array([])])
        b = np.concatenate([r[k:] for r in x if len(r) > k] or [np.array([])])
        if len(a) < 20:
            break
        rho = float((a * b).mean() / var)
        if rho <= 0.05:
            break
        lags.append(k)
        rhos.append(rho)
    # ρ(k) = exp(−k/τ), least squares through the origin on −ln ρ.
    tau = (sum(k * k for k in lags) / sum(-k * math.log(r) for k, r in zip(lags, rhos))
           if lags else DEFAULTS['tau'])
    return dict(fitted, var=var, tau=min(240.0, max(2.0, tau)))


def fit_voltage(rows):
    """Median loaded voltage per percent, interpolated over 0–100 (None if sparse)."""
    buckets = collections.defaultdict(list)
    for m in rows:
        if m.get('volts') and m.get('pct') is not None:
            buckets[int(m['pct'])].append(m['volts'])
    known = sorted((p, float(np.median(v))) for p, v in buckets.items() if len(v) >= 2)
    if len(known) < 5:
        return None
    pct, volts = zip(*known)
    # Monotone in charge: smooth with a quadratic, then never extrapolate wildly.
    coef = np.polyfit(pct, volts, 2)
    curve = np.polyval(coef, np.arange(101))
    return [float(v) for v in np.clip(curve, min(volts) - 0.3, max(volts) + 0.3)]


def fit_kappa(rows):
    """Charge counter ÷ integrated current: how much the current reading under-reads."""
    counted = measured = 0.0
    for run in runs(rows):
        for a, b in zip(run, run[1:]):
            if a.get('charge') and b.get('charge') and b.get('volts') and a.get('unit') != 'uWh':
                counted += (a['charge'] - b['charge']) / 1e6           # Ah
                measured += b['bat'] / b['volts'] * b['dt'] / 3600    # Ah
    return min(1.3, max(0.8, counted / measured)) if measured > 0.5 else 1.0


def fit_charging(minutes):
    """Percent per minute by state of charge, and spread of 10-minute log rate ratios."""
    rates = collections.defaultdict(list)
    # Full charge from the coulomb counter (capacity is its rounded ratio).
    estimates = [m['charge'] / (m['pct'] / 100) for m in minutes if m.get('charge') and (m.get('pct') or 0) >= 30]
    if not estimates:
        return None, DEFAULTS['charge_sigma']
    full = float(np.median(estimates))
    for run in runs([m for m in minutes if m['st'] == 'C' and m.get('charge')]):
        for a, b in zip(run, run[1:]):
            if b['charge'] > a['charge'] and a.get('pct') is not None:
                rates[int(a['pct'])].append((b['charge'] - a['charge']) / full * 100 / (b['t'] - a['t']) * 60)
    if sum(len(v) for v in rates.values()) < 20:
        return None, DEFAULTS['charge_sigma']
    curve = []
    for p in range(101):
        near = [r for q in range(p - 3, p + 4) for r in rates.get(q, [])]
        curve.append(float(np.median(near)) if len(near) >= 3 else None)
    known = [i for i, r in enumerate(curve) if r is not None]
    for p in range(101):  # Fill gaps from the nearest known percent.
        if curve[p] is None:
            curve[p] = curve[min(known, key=lambda q: abs(q - p))]
    ratios = []
    for run in runs([m for m in minutes if m['st'] == 'C' and m.get('charge')]):
        for i in range(0, len(run) - 10, 10):
            a, b = run[i], run[i + 10]
            if a.get('pct') and b['charge'] > a['charge'] and a['pct'] > 1:
                actual = (b['pct'] - a['pct']) / ((b['t'] - a['t']) / 60)
                expected = np.mean(curve[int(a['pct']):int(b['pct']) + 1])
                if actual > 0 and expected > 0:
                    ratios.append(math.log(actual / expected))
    sigma = float(np.std(ratios)) if len(ratios) >= 5 else DEFAULTS['charge_sigma']
    return [max(0.02, r) for r in curve], max(0.05, sigma)


def deaths_in(minutes, running=None):
    """Battery deaths the minutes show: each boot that ended on battery below
    CUTOFF_PCT, cut at its last minute's charge less 5 s of the falling rate
    (records are 10 s apart, so the cut came 0–10 s later). `running` (by
    default the boot of the latest minute) hasn't ended."""
    by_boot = collections.defaultdict(list)
    for m in minutes:
        if m.get('boot') and m.get('charge') is not None:
            by_boot[m['boot']].append(m)
    running = running or (max(by_boot, key=lambda b: by_boot[b][-1]['t']) if by_boot else None)
    deaths = []
    for boot, ms in by_boot.items():
        last = ms[-1]
        if boot == running or last['st'] != 'D' or (last.get('pct') or 100) >= CUTOFF_PCT:
            continue
        before = [m for m in ms if m['st'] == 'D' and m['t'] >= last['t'] - 180]
        span = last['t'] - before[0]['t']
        rate = (before[0]['charge'] - last['charge']) / 1e6 / span if span > 0 else 0.0  # Ah/s
        deaths.append({'boot': boot, 't': last['t'] + 60, 'cut': last['charge'] / 1e6 - 5 * max(rate, 0.0),
                       'source': 'records'})
    return deaths


def fit_cutoff(minutes, known=(), running=None):
    """The charge (Ah) at which the battery cuts out, its spread, and how many
    deaths say so: `known` deaths (deaths.json: black-box gasps, timed within
    2 s, and the ones remembered from records) and those the minutes show.
    A boot battery-eta powered off itself (`poweroff`) didn't die."""
    known = {d['boot']: d for d in known}
    deaths = [d for d in known.values() if not d.get('poweroff')]
    deaths += [d for d in deaths_in(minutes, running) if d['boot'] not in known]
    cuts = [d['cut'] for d in deaths if d.get('cut') is not None]
    if not cuts:
        return {k: DEFAULTS[k] for k in ('cutoff', 'cutoff_spread', 'deaths')}
    middle = float(np.median(cuts))
    spread = 1.4826 * float(np.median(np.abs(np.array(cuts) - middle)))  # The MAD, as a standard deviation.
    return {'cutoff': middle, 'cutoff_spread': max(spread, 0.001), 'deaths': len(cuts)}


def load_gasps(path=None):
    """deaths.json: every death (and power-off) remembered, past the 30-day window."""
    try:
        gasps = json.loads((path or DEATHS).read_text())
        return gasps if isinstance(gasps, list) else []
    except (OSError, ValueError):
        return []


def remember(entries, path=None):
    """deaths.json with `entries` added (written only when there are any)."""
    path = path or DEATHS
    known = {e['boot']: e for e in load_gasps(path)}
    if entries:
        known.update({e['boot']: e for e in entries})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sorted(known.values(), key=lambda e: e.get('t', 0))[-100:]))
        except OSError as error:
            print(f'battery-eta: {path}: {error}', file=sys.stderr)
    return list(known.values())


def remember_deaths(minutes, running=None, path=None):
    """deaths.json, with the deaths these minutes show added (the window moves on)."""
    known = {e['boot'] for e in load_gasps(path)}
    return remember([d for d in deaths_in(minutes, running) if d['boot'] not in known], path)


def harvest_gasp(boot, gasp_path=None, deaths_path=None):
    """At start: a last gasp from an earlier boot. One battery-eta wrote as it
    powered the laptop off is remembered as that (not a death); one close
    enough to the cutoff was a death, cut within 2 s of it (a shutdown by hand,
    further from it, was neither). The gasp is removed either way."""
    gasp_path, deaths_path = gasp_path or GASP, deaths_path or DEATHS
    try:
        gasp = json.loads(gasp_path.read_text())
    except (OSError, ValueError):
        return None
    if gasp.get('boot') == boot:
        return None  # Still this boot's: battery-eta restarted.
    gasp_path.unlink(missing_ok=True)
    if gasp.get('poweroff'):
        entry = {'boot': gasp['boot'], 't': gasp['t'], 'poweroff': True, 'charge': gasp['charge']}
    elif (gasp.get('seconds') is not None and gasp['seconds'] <= 120) or gasp['charge'] <= 0.025 * gasp['full']:
        entry = dict(gasp, cut=gasp['charge'] - (gasp.get('rate') or 0.0), source='black box')
    else:
        return None
    remember([entry], deaths_path)
    return entry


def fit(minutes, known=None):
    """Every battery-eta model from `minutes`; `known` deaths default to deaths.json (read only)."""
    params = dict(DEFAULTS)
    battery = [m for m in minutes if m['st'] == 'D' and m.get('bat', 0) > 0 and m['dt'] >= 40]
    if (ou := fit_ou(battery)) is not None:
        params.update(ou)
    params['volts'] = fit_voltage(battery)
    params['kappa'] = fit_kappa(battery)
    params['charge_curve'], params['charge_sigma'] = fit_charging(minutes)
    params.update(fit_cutoff(minutes, load_gasps() if known is None else known, power.boot_id()))
    params['fitted'] = time.time()
    return params


MODELS = report.LOG / 'models.json'
MODELS_VERSION = 3  # 3: the chip model gained the clock term; the draw forecast went to log watts.
WINDOW_DAYS = 30


def train(minutes, events, now=None, known=None):
    """Every model, from one window of history. This is the only place models
    are trained: the collector's battery-use breakdown and the battery panel
    read the result from models.json instead of fitting their own."""
    rows = report.battery_rows(minutes)
    params = fit(minutes, known)
    params['sleep'] = report.sleep_model(report.sleep_drain(events))
    params['attribution'] = {name: float(value) for name, value in report.fit_rows(rows).items()}
    params.update(fitted=now or time.time(), window_days=WINDOW_DAYS, battery_minutes=len(rows))
    return params


def save_models(params, path=None):
    path = path or MODELS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': MODELS_VERSION, **params}))
    temporary.replace(path)


def load_models(path=None, max_age=3600):
    """The shared models if trained within `max_age` seconds, else None."""
    try:
        data = json.loads((path or MODELS).read_text())
    except (OSError, ValueError):
        return None
    if data.get('version') != MODELS_VERSION or time.time() - data.get('fitted', 0) > max_age:
        return None
    return data


# --- Forecasts --------------------------------------------------------------------

def usable_energy(charge_ah, full_ah, params):
    """Wh left above the cutoff."""
    floor = remaining_energy(min(params.get('cutoff') or 0.0, charge_ah) / full_ah, full_ah, params['volts'])
    return max(0.0, remaining_energy(charge_ah / full_ah, full_ah, params['volts']) - floor)


def remaining_energy(fraction, full_ah, volts):
    """Wh left above empty: ∫ V(q) dq from 0 to the current charge fraction."""
    if not volts:
        return fraction * full_ah * 15.4
    pct = fraction * 100
    whole = int(min(pct, 100))
    energy = sum(volts[:whole]) / 100 * full_ah
    if whole < 100:
        energy += (pct - whole) / 100 * full_ah * volts[whole]
    return energy


def draw_lognormal(params, recent, minutes):
    """(μ, σ) of log average draw over the next `minutes`."""
    tau, var, level = params['tau'], params['var'], params['level']
    x = max(minutes, 1e-3) / tau
    carry = (1 - math.exp(-x)) / x                       # Share of today's deviation that persists.
    mu = level + (math.log(max(recent, 0.1)) - level) * carry
    spread = var / x ** 2 * (2 * x - 3 + 4 * math.exp(-x) - math.exp(-2 * x))
    spread += (1 - carry) ** 2 * var * 2 * tau / max(params['level_n'], 1)  # The level is estimated.
    return max(mu, math.log(params['floor'])), math.sqrt(spread)


def typical_draw(params):
    """Battery watts with nothing recent to go on: the local level."""
    return math.exp(params['level'])


def horizon_draw(params, recent, minutes):
    """Median and 80% interval of the average draw over the next `minutes` (log-normal)."""
    mu, sigma = draw_lognormal(params, recent, minutes)
    return math.exp(mu), math.exp(mu - Z80 * sigma), math.exp(mu + Z80 * sigma)


def normal_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def grid(low, high, points):
    start, stop = max(low * 0.4, 1 / 60), max(high * 1.8, low * 0.4 + 1 / 30)
    return [start + (stop - start) * i / (points - 1) for i in range(points)]


def density(times, cdf):
    """Density from a CDF on a grid (central differences, never negative)."""
    pdf = []
    for i in range(len(times)):
        a, b = max(0, i - 1), min(len(times) - 1, i + 1)
        pdf.append(max(0.0, (cdf[b] - cdf[a]) / (times[b] - times[a])))
    return pdf


def empty_distribution(params, energy, recent, low, high, points=96):
    """Time to empty (hours) as (times, density, cdf).

    The battery is empty by t exactly when the average draw over [0, t] reaches
    energy / t, so P(T ≤ t) = P(κ·draw_t ≥ energy / t) under the same log-normal
    horizon forecast that gives the median and interval.
    """
    times, cdf = grid(low, high, points), []
    for t in times:
        mu, sigma = draw_lognormal(params, recent, t * 60)
        cdf.append(1 - normal_cdf((math.log(energy / (params['kappa'] * t)) - mu) / sigma))
    return times, density(times, cdf), cdf


def full_distribution(median, sigma, low, high, points=96):
    """Time to the charge limit (hours): log-normal around the curve's estimate."""
    times = grid(low, high, points)
    cdf = [normal_cdf((math.log(t) - math.log(median)) / sigma) for t in times]
    return times, density(times, cdf), cdf


def time_to_empty(params, energy, recent):
    """Hours (median, low, high): solve T = energy / draw over T for each quantile."""
    result = []
    for index in (0, 2, 1):  # Median, then the high-draw (short) and low-draw (long) ends.
        hours = energy / max(recent, 1)
        for _ in range(40):
            draw = horizon_draw(params, recent, hours * 60)[index] * params['kappa']
            new = energy / max(draw, 0.5)
            if abs(new - hours) < 1e-4:
                break
            hours = new
        result.append(hours)
    return tuple(result)


def use_per_charge(params, charge_watts, energy_full):
    """Minutes of battery use bought per minute of charging (median, low, high).

    Energy entering the battery (true watts, from the charge counter) divided by
    the draw it will later supply: the forecast average battery draw over a full
    charge's worth of use (the AC draw itself is not representative: AC runs the
    Balanced profile). The interval is that draw forecast's 80% interval.
    """
    typical = typical_draw(params)
    horizon = 60 * energy_full / (params['kappa'] * typical)
    median, low, high = (params['kappa'] * d for d in horizon_draw(params, typical, horizon))
    return charge_watts / median, charge_watts / high, charge_watts / low


def time_to_full(params, pct, target, rate_now):
    """Minutes (median, low, high) to reach `target` percent at the learned charge curve."""
    curve = params['charge_curve'] or [0.8 if p < 80 else max(0.2, 0.8 - (p - 80) * 0.06) for p in range(101)]
    if pct >= target - 0.5:
        return 0.0, 0.0, 0.0
    expected = curve[min(int(pct), 100)]
    scale = min(3.0, max(0.25, rate_now / expected)) if rate_now and rate_now > 0 else 1.0
    minutes = 0.0
    p = pct
    while p < target:
        step = min(1 - (p % 1) or 1, target - p)
        minutes += step / (curve[min(int(p), 100)] * scale)
        p += step
    sigma = params['charge_sigma']
    return minutes, minutes * math.exp(-Z80 * sigma), minutes * math.exp(Z80 * sigma)


# --- Live loop ----------------------------------------------------------------------

def countdown(seconds):
    """'4m 07s', or '41s' under a minute."""
    seconds = max(0, round(seconds))
    return f'{seconds // 60}m {seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


def clock(hours):
    if not math.isfinite(hours) or hours > 99:
        return '--'
    minutes = round(hours * 60)
    return f'{minutes // 60}:{minutes % 60:02d}'


# Font Awesome glyphs as escapes: private-use characters are invisible in most
# editors and were once silently lost, leaving the charging bolt blank.
BOLT, PLUG, HOURGLASS, POWER = '\uf0e7', '\uf1e6', '\uf252', '\uf011'
APPROX = "<span size='small' alpha='70%'>~</span>"


def icon(glyph):
    return f"<span font_family='Font Awesome 7 Free' weight='heavy' size='small'>{glyph}</span>"


def plus_minus(low, high):
    """Half the 80% interval as one ± figure (the interval is close to symmetric)."""
    return f"<span size='small' alpha='70%'>±{clock((high - low) / 2)}</span>"


RECOMMEND_KEYS = ('text', 'apply', 'change', 'gain', 'interval', 'delight')


def write_recommendations(minutes, events, path=None):
    """Each goal's best measured combination (report.recommendations) for the
    battery menu and `power-experiment goal --apply`: recommend.json."""
    rows = report.battery_rows(minutes)
    power = sum(m['bat'] for m in rows) / len(rows) if rows else None
    energy = report.full_energy(minutes)
    levers = report.lever_effects(minutes, energy, power, replicates=100)
    recs = report.recommendations(levers, report.qol_model(minutes, events), energy, power)
    path = path or RUNTIME / 'recommend.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    written = {'updated': time.time(), **{goal: {k: r[k] for k in RECOMMEND_KEYS} for goal, r in recs.items()}}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(written))
    temporary.replace(path)
    return written


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


class Live:
    def __init__(self, sys_root=Path('/sys'), proc=Path('/proc'), gasp=None, notify=None, poweroff=None):
        self.battery = find_battery(sys_root / 'class/power_supply')
        self.samples = collections.deque()
        self.params, self.loading = dict(DEFAULTS), False
        self.boot, self.gasp = power.boot_id(proc), gasp or GASP
        self.notify, self.warned = notify or send_notification, set()
        self.poweroff, self.due, self.powering_off = poweroff or power_off, 0, False

    def refit(self):
        minutes, events = report.load_range(time.time() - WINDOW_DAYS * 86400, time.time() + 60)
        # Deaths in these minutes join deaths.json, so the cutoff outlives the window.
        self.params = train(minutes, events, known=remember_deaths(minutes, self.boot))
        save_models(self.params)
        try:
            write_recommendations(minutes, events)
        except Exception as error:  # noqa: BLE001 - the menu's lines must never stop the estimate
            print(f'battery-eta: recommendations failed: {error}', file=sys.stderr)
        # Seed the recent window from the log so a restart is not blind.
        if not self.samples:
            for m in minutes[-15:]:
                if m['st'] == 'D' and m.get('bat'):
                    self.samples.append((m['t'] + 60, m['bat'], 'Discharging', None))

    def sample(self):
        b = self.battery
        status = read(b / 'status') or 'Unknown'
        try:
            current, volts = int(read(b / 'current_now')) / 1e6, int(read(b / 'voltage_now')) / 1e6
            charge, full = int(read(b / 'charge_now')) / 1e6, int(read(b / 'charge_full')) / 1e6
        except (TypeError, ValueError):
            return None
        now = time.time()
        self.samples.append((now, current * volts, status, charge))
        while self.samples and self.samples[0][0] < now - 900:
            self.samples.popleft()
        return {'status': status, 'watts': current * volts, 'charge': charge, 'full': full, 'volts': volts,
                'pct': 100 * charge / full if full else 0, 'limit': report_number(b / 'charge_control_end_threshold'),
                'capacity': report_number(b / 'capacity')}

    def recent(self, status, seconds):
        now = time.time()
        values = [w for t, w, s, _ in self.samples if s == status and t >= now - seconds]
        return sum(values) / len(values) if values else None

    def gauge_trend(self, seconds=120):
        """(charge now, Ah per second falling) from the gauge's own readings over
        the last `seconds` on battery: it lowers its estimate faster under heavy
        draw, which the current alone misses. None with under a minute of them."""
        now = time.time()
        points = [(t - now, q) for t, _, s, q in self.samples
                  if s == 'Discharging' and q is not None and t >= now - seconds]
        if len(points) < 10 or points[-1][0] - points[0][0] < 60:
            return None
        slope, level = np.polyfit(*np.array(points).T, 1)
        return (float(level), float(-slope)) if slope < 0 else None

    def record_gasp(self, state, seconds, rate):
        """The black box: below LOW_BATTERY, the last reading on disk every 2 s
        (marked once battery-eta is powering the laptop off: that isn't a death)."""
        gasp = {'t': time.time(), 'boot': self.boot, 'charge': state['charge'], 'full': state['full'],
                'volts': state['volts'], 'watts': state['watts'], 'rate': rate, 'seconds': seconds,
                'poweroff': self.powering_off}
        try:
            self.gasp.parent.mkdir(parents=True, exist_ok=True)
            with open(self.gasp, 'w') as handle:
                json.dump(gasp, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            print(f'battery-eta: last gasp: {error}', file=sys.stderr)

    def clear_gasp(self):
        """Plugged in: not a death, and the warnings start over."""
        self.warned.clear()
        self.due, self.powering_off = 0, False
        if self.gasp.exists():
            self.gasp.unlink(missing_ok=True)

    def warns_off(self):
        """Once a death has taught the cutoff, battery-eta powers off before it."""
        return self.params.get('deaths', 0) >= 1

    def warn(self, seconds, pm):
        """A notification as the end comes within each of WARN_SECONDS."""
        for limit in WARN_SECONDS:
            if seconds <= limit and limit not in self.warned:
                self.warned.update(w for w in WARN_SECONDS if w >= limit)
                at = time.strftime('%H:%M:%S', time.localtime(time.time() + seconds))
                if self.warns_off():
                    self.notify(f'Powering off at {at}', f'In {countdown(seconds)} (±{pm:.0f} s), '
                                f'{POWEROFF_SECONDS} s before the battery cuts out. Plug in to stop it.')
                else:
                    self.notify(f'Battery cuts out at {at}',
                                f'In {countdown(seconds)} (±{pm:.0f} s). Plug in, or save your work.')
                return

    def maybe_power_off(self, state, cutout, rate):
        """POWEROFF_SECONDS before the cutoff — twice in a row, as the gauge steps,
        and within 10 mAh of it, so a wild trend can't — power off cleanly: apps
        close, the disks settle, the journal keeps its tail."""
        due = (self.warns_off() and cutout <= POWEROFF_SECONDS and state['pct'] < LOW_BATTERY
               and state['charge'] - self.params['cutoff'] <= 0.010)
        self.due = self.due + 1 if due else 0
        if self.due >= 2 and not self.powering_off:
            self.powering_off = True
            self.record_gasp(state, cutout, rate)
            self.notify('Powering off', f'The battery cuts out in {countdown(cutout)}.')
            self.poweroff()

    def charge_rate(self, full):
        """Percent per minute over the last five minutes of charging (charge counter)."""
        points = [(t, q) for t, _, s, q in self.samples
                  if s == 'Charging' and q is not None and t >= time.time() - 300]
        if len(points) < 2 or points[-1][0] - points[0][0] < 60:
            return None
        (t0, q0), (t1, q1) = points[0], points[-1]
        return (q1 - q0) / full * 100 / ((t1 - t0) / 60)

    def estimate(self):
        state = self.sample()
        if state is None:
            return {'text': '--', 'class': ['unknown']}
        params = self.params
        energy_full = usable_energy(state['full'], state['full'], params)
        energy = usable_energy(state['charge'], state['full'], params)
        fill = round(100 * energy / energy_full) if energy_full else round(state['pct'])
        result = {'status': state['status'], 'pct': state['pct'], 'energy_pct': fill, 'watts': state['watts'],
                  'energy_wh': energy, 'updated': time.time()}
        classes = [f'fill{max(0, min(100, fill))}']
        if (asleep := report.sleep_runtime([], energy, model=params.get('sleep'))) is not None:
            result['sleep'] = asleep  # How long a suspend from now would last.
        if state['status'] == 'Discharging':
            # A short session borrows from the local level instead of trusting a few seconds.
            recent = self.recent('Discharging', 600)
            n = sum(1 for t, _, s, _ in self.samples if s == 'Discharging' and t >= time.time() - 600)
            weight = min(1.0, n / 60)
            typical = typical_draw(params)
            draw = (recent or typical) * weight + typical * (1 - weight)
            median, low, high = time_to_empty(params, energy, draw)
            times, pdf, _ = empty_distribution(params, energy, draw, low, high)
            trend = self.gauge_trend()
            rate = trend[1] if trend else None
            if trend and median * 60 < FINAL_MINUTES:
                # The gauge's own trend to the cutoff; ± where the cutoff falls (two spreads).
                seconds = max(0.0, (trend[0] - params['cutoff']) / rate)
                pm = max(10.0, 2 * params['cutoff_spread'] / rate)
                median, low, high = seconds / 3600, max(0.0, seconds - pm) / 3600, (seconds + pm) / 3600
                self.maybe_power_off(state, seconds, rate)
            seconds, pm = median * 3600, (high - low) * 1800
            # The end the user sees: the power-off, once there is one, else the cutoff.
            end = max(0.0, seconds - POWEROFF_SECONDS) if self.warns_off() else seconds
            result.update(hours=median, low=low, high=high, draw=draw)
            result['distribution'] = {'kind': 'empty', 't': [round(t, 4) for t in times],
                                      'p': [round(v, 5) for v in pdf]}
            classes.append('discharging')
            if median < 0.5 or fill <= 10:
                classes.append('low')
            if median * 60 < COUNTDOWN_MINUTES:
                result.update(cutout=time.time() + seconds, cutout_pm=pm)
                if self.warns_off():
                    result['poweroff'] = time.time() + end
                self.warn(end, pm)
                if median * 60 < FINAL_MINUTES:
                    classes.append('critical')
                glyph = POWER if self.warns_off() else HOURGLASS
                text = (f"{icon(glyph)} {countdown(end)} <span size='small' alpha='70%'>±{pm:.0f}s</span> "
                        f"<span alpha='70%'>{state['watts']:.1f}W</span>")
            else:
                text = (f"{icon(HOURGLASS)} {clock(median)} {plus_minus(low, high)} "
                        f"<span alpha='70%'>{state['watts']:.1f}W</span>")
            if state['pct'] < LOW_BATTERY:
                self.record_gasp(state, seconds, rate)
        elif state['status'] == 'Charging':
            self.clear_gasp()
            target = state['limit'] or 100
            rate = self.charge_rate(state['full'])
            median, low, high = time_to_full(params, state['pct'], target, rate)
            result.update(hours=median / 60, low=low / 60, high=high / 60, target=target)
            if median > 0:
                times, pdf, _ = full_distribution(median / 60, params['charge_sigma'], low / 60, high / 60)
                result['distribution'] = {'kind': 'full', 't': [round(t, 4) for t in times],
                                          'p': [round(v, 5) for v in pdf]}
            # Into the battery, in true watts: the counter's rate, else the calibrated reading.
            charge_watts = (rate / 100 * state['full'] * 60 * state['volts'] if rate
                            else abs(state['watts']) * params['kappa'])
            ratio, ratio_low, ratio_high = use_per_charge(params, charge_watts, energy_full)
            result.update(ratio=ratio, ratio_low=ratio_low, ratio_high=ratio_high, charge_watts=charge_watts)
            # Time on battery banked so far: the current energy at the typical
            # battery draw (not the AC draw), the same number shown while discharging.
            banked, banked_low, banked_high = time_to_empty(params, energy, typical_draw(params))
            result.update(banked=banked, banked_low=banked_low, banked_high=banked_high)
            classes.append('charging')
            # Both ranges are wide (≈±20% / ±30%), so the label shows ~ and leaves them to the menu.
            text = (f"{icon(BOLT)} {APPROX}{clock(banked)} <span size='small' alpha='70%'>full ~{clock(median / 60)}</span> "
                    f"<span size='small' alpha='70%'>{abs(state['watts']):.0f}W</span>")
        else:
            self.clear_gasp()
            classes.append('plugged')
            limit = state['limit'] or 100
            held = state['pct'] >= limit - 1.5
            text = f"{icon(PLUG)} " + ('held at limit' if held else 'on AC')
        result['text'], result['classes'] = text, classes
        return {'text': text, 'class': classes, 'percentage': fill, 'detail': result}


def power_off():
    try:
        subprocess.Popen(['systemctl', 'poweroff'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        print(f'battery-eta: systemctl poweroff: {error}', file=sys.stderr)


def send_notification(title, body):
    try:
        subprocess.Popen(['notify-send', '-u', 'critical', '-a', 'Battery', '-i', 'battery-empty', title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        print(f'battery-eta: notify-send: {error}', file=sys.stderr)


def report_number(path):
    try:
        return float(path.read_text())
    except (OSError, ValueError):
        return None


def main():
    live = Live()
    if (last := harvest_gasp(live.boot)) is not None:
        print(f"battery-eta: the last boot {'powered off' if last.get('poweroff') else 'died'} at "
              f"{last['charge'] * 1000:.0f} mAh, {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last['t']))}",
              file=sys.stderr)
    # Start from the last trained models so the label appears at once; the
    # first training (which may read days of log into the SQLite store) follows.
    if (saved := load_models(max_age=float('inf'))) is not None:
        live.params = {**DEFAULTS, **saved}
    last_fit = 0.0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    while True:
        if time.time() - last_fit > REFIT and (last_fit or saved is None or live.samples):
            try:
                live.refit()
            except Exception as error:  # noqa: BLE001
                print(f'battery-eta: refit failed: {error}', file=sys.stderr)
            last_fit = time.time()
        output = live.estimate()
        detail = output.pop('detail', None)
        print(json.dumps(output), flush=True)
        if detail:
            temporary = RUNTIME / 'eta.tmp'
            shown = {k: v for k, v in live.params.items() if k in ('mean', 'tau', 'kappa', 'charge_sigma')}
            temporary.write_text(json.dumps(dict(detail, params=dict(shown, typical=typical_draw(live.params)))))
            temporary.replace(RUNTIME / 'eta.json')
        time.sleep(2)


if __name__ == '__main__':
    main()
