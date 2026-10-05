#!/usr/bin/env python3
"""Time to empty / time to full with intervals, for the Waybar battery outline.

Discharging. Remaining energy is ∫ V(q) dq over the remaining charge, with the
loaded-voltage curve V(q) learned from the power log (each percent of this
gauge's coulomb-counted charge holds ~15% less energy near empty than near full),
and the battery's current reading calibrated against its charge counter. Future
average draw over the horizon is forecast as an Ornstein–Uhlenbeck process
fitted to minute-level battery power: the recent draw decays toward the
long-run mean with a learned time constant, and the variance of the horizon
average (plus uncertainty in the long-run mean) gives a log-normal interval.
The horizon is solved jointly with the answer (T = energy / mean draw over T).

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
import sys
import time
from pathlib import Path

import numpy as np

import report
from power import find_battery

Z80 = 1.2816  # 80% interval: 10th–90th percentile.
REFIT = 15 * 60
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
DEFAULTS = {'mean': 15.0, 'var': 25.0, 'tau': 20.0, 'observed': 60.0, 'kappa': 1.0, 'volts': None,
            'charge_curve': None, 'charge_sigma': 0.25, 'floor': 4.0}


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
    """Long-run mean/variance and autocorrelation time (minutes) of battery power."""
    x = [np.array([m['bat'] for m in run]) for run in runs(rows)]
    allx = np.concatenate(x) if x else np.array([])
    if len(allx) < 30:
        return None
    mean, var = float(allx.mean()), float(allx.var())
    lags, rhos = [], []
    for k in range(1, max_lag + 1):
        a = np.concatenate([r[:-k] - mean for r in x if len(r) > k] or [np.array([])])
        b = np.concatenate([r[k:] - mean for r in x if len(r) > k] or [np.array([])])
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
    return {'mean': mean, 'var': var, 'tau': min(240.0, max(2.0, tau)), 'observed': float(len(allx)),
            'floor': float(np.percentile(allx, 2))}


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


def fit(minutes):
    params = dict(DEFAULTS)
    battery = [m for m in minutes if m['st'] == 'D' and m.get('bat', 0) > 0 and m['dt'] >= 40]
    if (ou := fit_ou(battery)) is not None:
        params.update(ou)
    params['volts'] = fit_voltage(battery)
    params['kappa'] = fit_kappa(battery)
    params['charge_curve'], params['charge_sigma'] = fit_charging(minutes)
    params['fitted'] = time.time()
    return params


MODELS = report.LOG / 'models.json'
WINDOW_DAYS = 30


def train(minutes, events, now=None):
    """Every model, from one window of history. This is the only place models
    are trained: the collector's battery-use breakdown and the battery panel
    read the result from models.json instead of fitting their own."""
    rows = report.battery_rows(minutes)
    params = fit(minutes)
    params['sleep'] = report.sleep_model(report.sleep_drain(events))
    params['attribution'] = {name: float(value) for name, value in report.fit_rows(rows).items()}
    params.update(fitted=now or time.time(), window_days=WINDOW_DAYS, battery_minutes=len(rows))
    return params


def save_models(params, path=None):
    path = path or MODELS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': 2, **params}))
    temporary.replace(path)


def load_models(path=None, max_age=3600):
    """The shared models if trained within `max_age` seconds, else None."""
    try:
        data = json.loads((path or MODELS).read_text())
    except (OSError, ValueError):
        return None
    if data.get('version') != 2 or time.time() - data.get('fitted', 0) > max_age:
        return None
    return data


# --- Forecasts --------------------------------------------------------------------

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
    tau, var, mean = params['tau'], params['var'], params['mean']
    x = max(minutes, 1e-3) / tau
    carry = (1 - math.exp(-x)) / x                       # Share of today's deviation that persists.
    centre = mean + (recent - mean) * carry
    spread = var / x ** 2 * (2 * x - 3 + 4 * math.exp(-x) - math.exp(-2 * x))
    spread += (1 - carry) ** 2 * var * 2 * tau / max(params['observed'], 1)  # Long-run mean is estimated.
    centre = max(centre, params['floor'])
    sigma = math.sqrt(math.log(1 + spread / centre ** 2))
    return math.log(centre) - sigma ** 2 / 2, sigma


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
    horizon = 60 * energy_full / (params['kappa'] * params['mean'])
    median, low, high = (params['kappa'] * d for d in horizon_draw(params, params['mean'], horizon))
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

def clock(hours):
    if not math.isfinite(hours) or hours > 99:
        return '--'
    minutes = round(hours * 60)
    return f'{minutes // 60}:{minutes % 60:02d}'


# Font Awesome glyphs as escapes: private-use characters are invisible in most
# editors and were once silently lost, leaving the charging bolt blank.
BOLT, PLUG, HOURGLASS = '\uf0e7', '\uf1e6', '\uf252'
APPROX = "<span size='small' alpha='70%'>~</span>"


def icon(glyph):
    return f"<span font_family='Font Awesome 7 Free' weight='heavy' size='small'>{glyph}</span>"


def plus_minus(low, high):
    """Half the 80% interval as one ± figure (the interval is close to symmetric)."""
    return f"<span size='small' alpha='70%'>±{clock((high - low) / 2)}</span>"


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


class Live:
    def __init__(self, sys_root=Path('/sys')):
        self.battery = find_battery(sys_root / 'class/power_supply')
        self.samples = collections.deque()
        self.params, self.loading = dict(DEFAULTS), False

    def refit(self):
        minutes, events = report.load_range(time.time() - WINDOW_DAYS * 86400, time.time() + 60)
        self.params = train(minutes, events)
        save_models(self.params)
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
        energy_full = remaining_energy(1.0, state['full'], params['volts'])
        energy = remaining_energy(state['pct'] / 100, state['full'], params['volts'])
        fill = round(100 * energy / energy_full) if energy_full else round(state['pct'])
        result = {'status': state['status'], 'pct': state['pct'], 'energy_pct': fill, 'watts': state['watts'],
                  'energy_wh': energy, 'updated': time.time()}
        classes = [f'fill{max(0, min(100, fill))}']
        if (asleep := report.sleep_runtime([], energy, model=params.get('sleep'))) is not None:
            result['sleep'] = asleep  # How long a suspend from now would last.
        if state['status'] == 'Discharging':
            # A short session borrows from the long-run mean instead of trusting a few seconds.
            recent = self.recent('Discharging', 600)
            n = sum(1 for t, _, s, _ in self.samples if s == 'Discharging' and t >= time.time() - 600)
            weight = min(1.0, n / 60)
            draw = (recent or params['mean']) * weight + params['mean'] * (1 - weight)
            median, low, high = time_to_empty(params, energy, draw)
            result.update(hours=median, low=low, high=high, draw=draw)
            times, pdf, _ = empty_distribution(params, energy, draw, low, high)
            result['distribution'] = {'kind': 'empty', 't': [round(t, 4) for t in times],
                                      'p': [round(v, 5) for v in pdf]}
            classes.append('discharging')
            if median < 0.5 or fill <= 10:
                classes.append('low')
            text = (f"{icon(HOURGLASS)} {clock(median)} {plus_minus(low, high)} "
                    f"<span alpha='70%'>{state['watts']:.1f}W</span>")
        elif state['status'] == 'Charging':
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
            banked, banked_low, banked_high = time_to_empty(params, energy, params['mean'])
            result.update(banked=banked, banked_low=banked_low, banked_high=banked_high)
            classes.append('charging')
            # Both ranges are wide (≈±20% / ±30%), so the label shows ~ and leaves them to the menu.
            text = (f"{icon(BOLT)} {APPROX}{clock(banked)} <span size='small' alpha='70%'>full ~{clock(median / 60)}</span> "
                    f"<span size='small' alpha='70%'>{abs(state['watts']):.0f}W</span>")
        else:
            classes.append('plugged')
            limit = state['limit'] or 100
            held = state['pct'] >= limit - 1.5
            text = f"{icon(PLUG)} " + ('held at limit' if held else 'on AC')
        result['text'], result['classes'] = text, classes
        return {'text': text, 'class': classes, 'percentage': fill, 'detail': result}


def report_number(path):
    try:
        return float(path.read_text())
    except (OSError, ValueError):
        return None


def main():
    live = Live()
    # Start from the last trained models so the label appears at once; the
    # first training (which may convert days of log to Parquet) follows.
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
            temporary.write_text(json.dumps(dict(detail, params={k: v for k, v in live.params.items()
                                                                  if k in ('mean', 'tau', 'kappa', 'charge_sigma')})))
            temporary.replace(RUNTIME / 'eta.json')
        time.sleep(2)


if __name__ == '__main__':
    main()
