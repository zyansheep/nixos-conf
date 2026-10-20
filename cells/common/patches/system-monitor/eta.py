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
time constant. The median of the horizon's average draw sits above that path by
half the variance the horizon has averaged away, up to the mean draw (level +
var/2) over long horizons: a log-normal draw averages above its median, and
without that the time runs ~3% long. The variance of the horizon average (plus
the level's own uncertainty) gives a log-normal interval, scaled per horizon so
it holds 80% of what followed in the training window (fit_scale). Backtested
day by day against the 30-day watts version: ~0.5 W closer over the next hour,
on a par with the Chronos-2 forecasting model. The horizon is solved jointly
with the answer (T = energy / mean draw over T).

Empty is where the embedded controller cuts the power, not zero charge: it
switches the laptop off with ~55 mAh still on the gauge (1.8% of full), so
energy counts down to that cutoff, learned from every battery death
(fit_cutoff) and only from real ones: the journal says how each boot ended
(boot endings: a crash, a reboot, a shutdown or a death while suspended below
CUTOFF_PCT is not a cut). Below LOW_BATTERY the gauge's own falling charge over
the last few minutes times the end (the watts miss how much faster the gauge
falls near empty), and in the last FINAL_MINUTES its slope over two minutes
gives the second it cuts out (backtested on five deaths, leaving each out:
within ~10 s on average, 35 s at worst, from two minutes before). Below
LOW_BATTERY a black box fsyncs the last reading every 2 s (lastgasp.json), so
the next boot learns exactly when it died. Once a death has taught the cutoff,
battery-eta powers the laptop off cleanly once the early edge of the cutoff is
POWEROFF_SECONDS away (and counts down to that), recording the power-off so it
isn't taken for a death — unless the battery cut the shutdown short.

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
collector's menu breakdown and the battery panel read. It trains in a
background thread, so the black box, warnings and power-off never wait for it,
and not at all below LOW_BATTERY.

`battery-eta` prints one Waybar JSON line every 2 s (time, ± half the 80%
interval, then watts) and mirrors the estimate to
$XDG_RUNTIME_DIR/waybar-monitor/eta.json for the battery panel.
"""
import bisect
import collections
import json
import math
import os
import re
import subprocess
import sys
import threading
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
RETRY = 60  # s: a refit that failed, or found the database mid-migration, tries again this soon.
MIN_ROWS = 60  # Battery minutes a refit needs; with fewer the draw and attribution models stay as they were.
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
LEVEL_MINUTES = 300  # The local level: the last ~5 hours on battery (the best window in backtests of 100–1000).
SCALE_MINUTES = (2, 5, 10, 15, 30, 60, 120, 240)  # Horizons fit_scale calibrates (log-interpolated between).
DEFAULTS = {'mean': 15.0, 'level': math.log(15.0), 'level_n': 60, 'level_var': None, 'var': 0.08, 'tau': 20.0,
            'scale': None, 'kappa': 1.0, 'volts': None, 'charge_curve': None, 'charge_sigma': 0.25, 'floor': 4.0,
            'cutoff': 0.0, 'cutoff_spread': 0.003, 'deaths': 0}  # Ah: until a battery death says otherwise.
DRAW_KEYS = ('mean', 'level', 'level_n', 'level_var', 'var', 'tau', 'scale', 'floor', 'volts', 'kappa')
CUTOFF_PCT = 3  # A boot that ended on battery below this percent was cut off (all five so far at 2%).
FINAL_MINUTES = 3  # From here the gauge's slope over FINAL_SECONDS times the cutoff.
FINAL_SECONDS = 120
STEADY_SECONDS, STEADY_SPAN = 600, 300  # Below LOW_BATTERY: the gauge's slope over up to 10 minutes (5 at least).
COUNTDOWN_MINUTES = 10  # Below this, the label counts minutes and seconds.
LOW_BATTERY = 5  # Percent: below it the black box records every reading, and refits wait.
WARN_SECONDS = (300, 60)  # Notifications as the early edge of the end comes this close.
POWEROFF_SECONDS = 45  # A clean power-off this long before the cutoff's early edge, once a death has taught it.
DUE_SECONDS = 5  # The two readings that agree on the power-off come within this.
POWEROFF_RETRY = 10  # s between attempts when `systemctl poweroff` fails.
SUSPEND_GAP = 10  # s: a longer pause between estimates was a suspend (or a stall); the trends start over.
PLUGGED = ('Not charging', 'Full')
# s: mains online this long while the battery still discharges is a charger too weak to keep up, and no
# longer holds the power-off (on a plug-in the status has followed mains within one 10 s record).
WEAK_SECONDS = 60
GASP = store.STATE / 'lastgasp.json'  # The black box: the last reading, fsynced.
DEATHS = store.STATE / 'deaths.json'  # Every battery death its black box timed.


def boottime():
    """Seconds on a clock that never jumps and keeps counting through suspend."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def write_atomic(path, text):
    """`text` to `path` so a crash leaves the old file or the new one, never half of
    one: a temporary file, fsynced, renamed over it, and the directory fsynced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def backup(path):
    return path.with_name(path.name + '.bak')


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


def level_variance(lengths, var, tau):
    """Variance of the level, the mean log draw over runs of these lengths: within a
    run minutes correlate as e^(−lag/τ), runs not at all (≈ var·2τ/n for one long
    run, nearer var/n for many short ones)."""
    r, n = math.exp(-1 / tau), sum(lengths)
    pairs = sum(k * (1 + r) / (1 - r) - 2 * r * (1 - r ** k) / (1 - r) ** 2 for k in lengths)
    return var * pairs / n ** 2 if n else var


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
    pieces = [len(run) for run in runs(rows[-LEVEL_MINUTES:])]
    fitted = {'mean': float(allx.mean()), 'level': float(level.mean()), 'level_n': len(level),
              'floor': float(np.percentile(allx, 2))}
    if sum(len(r) for r in x) < 30:  # Too little history for deviations: the default variance and time constant.
        return dict(fitted, level_var=level_variance(pieces, DEFAULTS['var'], DEFAULTS['tau']))
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
    tau = min(240.0, max(2.0, tau))
    return dict(fitted, var=var, tau=tau, level_var=level_variance(pieces, var, tau))


def fit_scale(rows, params, step=5, shrink=50):
    """Factors on σ by horizon (SCALE_MINUTES) so the 80% interval holds 80% of what
    followed in the window, or None with under 50 outcomes.

    From every 5th minute of each run on battery (after its first 10): the forecast
    battery-eta would have made then (the level from the LEVEL_MINUTES before, the
    last 10 minutes as the recent draw) against the mean of the next battery minutes,
    across suspends and charging within a day, as a time to empty spans them. One
    time constant can't fit the two the draw has (ρ = 0.81 a minute on, 0.25 at 30):
    left alone the interval covers 77% at 10 minutes, 88% at 60, 73% at 4 hours.
    Each horizon's factor leans toward the pooled one by `shrink` outcomes.
    """
    xs = np.array([m['bat'] for m in rows], float)
    ts = np.array([m['t'] for m in rows], float)
    total = np.concatenate([[0.0], np.cumsum(np.log(np.maximum(xs, 0.1)))])
    watts = np.concatenate([[0.0], np.cumsum(xs)])
    lengths = [len(run) for run in runs(rows)]
    starts = np.concatenate([[0], np.cumsum(lengths)]).astype(int)
    plain = dict(params, scale=None)
    errors = {h: [] for h in SCALE_MINUTES}
    for k, length in enumerate(lengths):
        for i in range(10, length, step):
            g = starts[k] + i
            first = max(0, g - LEVEL_MINUTES)
            if g - first < 30:
                continue
            pieces, j = [], k
            while j >= 0 and starts[j + 1] > first:
                pieces.append(min(g, starts[j + 1]) - max(first, starts[j]))
                j -= 1
            plain.update(level=(total[g] - total[first]) / (g - first),
                         level_var=level_variance(pieces, params['var'], params['tau']))
            recent = (watts[g] - watts[g - 10]) / 10
            for h in SCALE_MINUTES:
                if g + h <= len(xs) and ts[g + h - 1] - ts[g] < 86400:
                    mu, sigma = draw_lognormal(plain, recent, h)
                    errors[h].append(abs(math.log((watts[g + h] - watts[g]) / h) - mu) / sigma)
    pooled = [e for h in SCALE_MINUTES for e in errors[h]]
    if len(pooled) < 50:
        return None
    middle = float(np.quantile(pooled, 0.8)) / Z80
    factors = [(len(e) * float(np.quantile(e, 0.8)) / Z80 + shrink * middle) / (len(e) + shrink) if e else middle
               for e in errors.values()]
    return {'minutes': list(SCALE_MINUTES), 'factor': [round(f, 4) for f in factors]}


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


# --- Boot endings ------------------------------------------------------------------

RESET = re.compile(r'Previous system reset reason \[(0x[0-9a-fA-F]+)\]')
RESET_REASONS = {0x08000800: 'sync flood', 0x00080800: 'reboot'}
ENDING_MESSAGES = (r'PM: suspend (entry|exit)|System is (powering down|rebooting)|'
                   r'Stopping Flush Journal to Persistent Storage|Power key pressed')


def journal(*args, timeout=20):
    """journalctl's output, or None if it can't run (exit 1 is --grep matching nothing)."""
    try:
        done = subprocess.run(['journalctl', '--no-pager', '-q', *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode in (0, 1) else None


class Endings:
    """How an earlier boot ended, from the system journal (wheel may read it):
    (how, when its journal ends), or None if the journal can't say now (ask
    again later). How is

    - 'shutdown': logind's 'System is powering down' (or rebooting), and the
      journal got as far as letting go of the disk, whatever reset reason the
      next boot then gives. 'shutdown cut short' if it didn't get that far.
    - 'sync flood', 'reboot', 'reset 0x…': the next boot's reset reason.
    - 'cut': the power vanished while it ran. A battery cut leaves the next boot no
      'Previous system reset reason' here, and there was no shutdown.
    - 'asleep': it died suspended (its last 'PM: suspend entry' has no exit).
    - 'power key': pressed in its last 30 s (held down, it cuts the power).
    - 'unknown': the journal works but doesn't have the boot (rotated away), so it
      never will say (and there is no end).

    Answers are cached; the past doesn't change.
    """

    def __init__(self, run=journal):
        self.run, self.cache, self.boots = run, {}, None

    def __call__(self, boot):
        if boot not in self.cache:
            how = self.classify(boot)
            if how is None:
                return None  # Ask again next time: the journal may be back.
            self.cache[boot] = how
        return self.cache[boot]

    def classify(self, boot):
        if not self.boots:
            try:
                self.boots = json.loads(self.run('--list-boots', '-o', 'json') or 'null')
            except ValueError:
                self.boots = None
        boots = self.boots or []
        if not boot or not boots:
            return None
        index = next((i for i, b in enumerate(boots) if str(b.get('boot_id', '')).startswith(boot)), None)
        if index is None:
            return 'unknown', None
        if index + 1 >= len(boots):
            return None  # Still running.
        this, after = boots[index], boots[index + 1]
        end = (this.get('last_entry') or 0) / 1e6
        reset = self.run('-k', '-b', after['boot_id'], '-o', 'cat', '--grep', 'Previous system reset reason')
        tail = self.run('-b', this['boot_id'], '-o', 'json', '--output-fields=MESSAGE', '--grep', ENDING_MESSAGES)
        if reset is None or tail is None:
            return None
        messages = []
        for line in tail.splitlines():
            try:
                entry = json.loads(line)
                messages.append((int(entry['__REALTIME_TIMESTAMP']) / 1e6, entry['MESSAGE']))
            except (ValueError, KeyError, TypeError):
                continue
        messages = [(t, m) for t, m in messages if isinstance(m, str)]
        down = [t for t, m in messages if m.startswith('System is ')]
        if down and any(t >= down[-1] and m.startswith('Stopping Flush Journal') for t, m in messages):
            return 'shutdown', end  # Finished, even if the next boot then names a reset (0x00200800 has followed one).
        if (match := RESET.search(reset)) is not None:
            return RESET_REASONS.get(int(match[1], 16), f'reset {match[1].lower()}'), end
        if down:
            return 'shutdown cut short', end
        sleeps = [m for _, m in messages if m.startswith('PM: suspend')]
        if sleeps and sleeps[-1].startswith('PM: suspend entry'):
            return 'asleep', end
        if any(m.startswith('Power key pressed') and t >= end - 30 for t, m in messages):
            return 'power key', end
        return 'cut', end


def deaths_in(minutes, running=None, endings=None, skip=()):
    """Battery deaths the minutes show: each boot that ended on battery below
    CUTOFF_PCT, cut at its last minute's charge less 5 s of the falling rate
    (records are 10 s apart, so the cut came 0–10 s later). `running` (by
    default the boot of the latest minute) hasn't ended; boots in `skip` are known.

    `endings` (Endings) says how each ended; one that wasn't a 'cut' (it crashed,
    rebooted, shut down or died asleep) comes back with its ending and no cut, so
    deaths.json remembers it isn't one. Without the journal every such boot counts;
    when the journal is there but can't say just now, the death counts
    `unconfirmed`, and the next refit asks again (confirm).
    """
    by_boot = collections.defaultdict(list)
    for m in minutes:
        if m.get('boot') and m.get('charge') is not None:
            by_boot[m['boot']].append(m)
    running = running or (max(by_boot, key=lambda b: by_boot[b][-1]['t']) if by_boot else None)
    deaths = []
    for boot, ms in by_boot.items():
        last = ms[-1]
        if (boot == running or boot in skip or last['st'] != 'D' or last.get('pct') is None
                or last['pct'] >= CUTOFF_PCT):
            continue
        how = endings(boot) if endings else None
        if how is not None and how[0] not in ('cut', 'unknown'):
            deaths.append({'boot': boot, 't': last['t'] + 60, 'ending': how[0], 'source': 'records'})
            continue
        before = [m for m in ms if m['st'] == 'D' and m['t'] >= last['t'] - 180]
        span = last['t'] - before[0]['t']
        rate = (before[0]['charge'] - last['charge']) / 1e6 / span if span > 0 else 0.0  # Ah/s
        deaths.append({'boot': boot, 't': last['t'] + 60, 'cut': last['charge'] / 1e6 - 5 * max(rate, 0.0),
                       'source': 'records', **({'ending': 'cut'} if how and how[0] == 'cut' else {}),
                       **({'unconfirmed': True} if endings and how is None else {})})
    return deaths


def number(value):
    """A finite number from a state file, else None (a hand-edited or damaged deaths.json)."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else None


def fit_cutoff(minutes, known=(), running=None, endings=None):
    """The charge (Ah) at which the battery cuts out, its spread, and how many
    deaths say so: `known` deaths (deaths.json: black-box gasps, timed within
    2 s, and the ones remembered from records) and those the minutes show.
    A boot battery-eta powered off itself (`poweroff`) didn't die, nor did one
    remembered with another ending (no `cut`)."""
    known = {d['boot']: d for d in known}
    deaths = [d for d in known.values() if not d.get('poweroff')]
    deaths += deaths_in(minutes, running, endings, skip=known)
    cuts = [c for d in deaths if (c := number(d.get('cut'))) is not None]
    if not cuts:
        return {k: DEFAULTS[k] for k in ('cutoff', 'cutoff_spread', 'deaths')}
    middle = float(np.median(cuts))
    spread = 1.4826 * float(np.median(np.abs(np.array(cuts) - middle)))  # The MAD, as a standard deviation.
    return {'cutoff': middle, 'cutoff_spread': max(spread, 0.001), 'deaths': len(cuts)}


def load_gasps(path=None):
    """deaths.json: every death (and power-off) remembered, past the 30-day window
    (its .bak if it won't parse); entries that aren't one (no boot) are left out."""
    path = path or DEATHS
    for candidate in (path, backup(path)):
        try:
            gasps = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(gasps, list):
            return [g for g in gasps if isinstance(g, dict) and isinstance(g.get('boot'), str)]
    return []


def remember(entries, path=None):
    """deaths.json with `entries` added (written only when there are any), atomically,
    the version before kept as deaths.json.bak."""
    path = path or DEATHS
    known = {e['boot']: e for e in load_gasps(path)}
    if entries:
        known.update({e['boot']: e for e in entries})
        try:
            try:
                previous = path.read_text()
                if isinstance(json.loads(previous), list):
                    write_atomic(backup(path), previous)
            except (OSError, ValueError):
                pass  # Nothing (good) to keep: the .bak stays as it was.
            write_atomic(path, json.dumps(sorted(known.values(), key=lambda e: number(e.get('t')) or 0.0)[-100:]))
        except OSError as error:
            print(f'battery-eta: {path}: {error}', file=sys.stderr)
    return list(known.values())


def remember_deaths(minutes, running=None, path=None, endings=None):
    """deaths.json, with the deaths (and non-deaths) these minutes show added (the
    window moves on), and those filed unconfirmed settled once the journal can say."""
    known = load_gasps(path)
    settled = confirm(known, endings) if endings else []
    return remember(settled + deaths_in(minutes, running, endings, skip={e['boot'] for e in known}), path)


def judge(gasp, how, end):
    """What a last gasp was, given how its boot ended (`how`, None or 'unknown'
    if the journal can't say, and when its journal ends): a death entry,
    battery-eta's finished power-off, or None (neither). See harvest_gasp."""
    charge, full = number(gasp.get('charge')), number(gasp.get('full')) or 0.0
    if charge is None:
        return None
    volts = number(gasp.get('volts')) or 15.4
    rate = number(gasp.get('rate')) or (number(gasp.get('watts')) or 0.0) / volts / 3600  # Ah/s
    left, t = number(gasp.get('seconds')), number(gasp.get('t'))
    near = (left is not None and left <= 120) or charge <= 0.025 * full
    if how in ('cut', 'shutdown cut short') or (how in (None, 'unknown') and not gasp.get('poweroff') and near):
        seconds = max(1.0, end - t) if how == 'shutdown cut short' and end and t is not None else 1.0
        cut = charge - rate * seconds
        if not cut < CUTOFF_PCT / 100 * full:
            return None
        return dict(gasp, cut=cut, source='black box', poweroff=False,
                    **({'ending': how} if how in ('cut', 'shutdown cut short') else {}))
    if gasp.get('poweroff') and how in (None, 'unknown', 'shutdown'):
        return {'boot': gasp['boot'], 't': t, 'poweroff': True, 'charge': charge}
    return None


def confirm(entries, endings):
    """Entries filed `unconfirmed` (the journal couldn't say then) that it can say
    about now, settled: a death, battery-eta's power-off, or neither (kept, with
    its ending, so it isn't asked again). The rest are left for later."""
    settled = []
    for e in entries:
        if not e.get('unconfirmed') or (answer := endings(e['boot'])) is None:
            continue
        how, end = answer
        if isinstance(e.get('gasp'), dict):
            entry = judge(e['gasp'], how, end)
        elif how in ('cut', 'unknown'):
            entry = {k: v for k, v in e.items() if k != 'unconfirmed'} | ({'ending': how} if how == 'cut' else {})
        else:
            entry = None
        settled.append(entry or {'boot': e['boot'], 't': e.get('t'), 'ending': how,
                                 'source': e.get('source') or ('black box' if 'gasp' in e else 'records')})
    return settled


def harvest_gasp(boot, gasp_path=None, deaths_path=None, endings=None):
    """At start: a last gasp from an earlier boot, removed either way.

    With the journal (`endings`): a 'cut' is a death within 2 s of the gasp's
    reading; a shutdown the battery cut short is one by the journal's last
    entry (the gasp's rate extrapolated), even battery-eta's own power-off. A
    crash, reboot, finished shutdown or death asleep is no death; battery-eta's
    finished power-off is remembered as that. Without the journal, as before:
    a power-off is one, and a gasp within 120 s or 2.5% of empty is a death;
    when the journal is there but can't say yet, that is filed `unconfirmed`
    with the gasp, and refits ask again (confirm). Either way a cut must be
    below CUTOFF_PCT: higher, it was some other failure (sync floods can leave
    no reset reason), and it would drag the cutoff up.
    """
    gasp_path, deaths_path = gasp_path or GASP, deaths_path or DEATHS
    try:
        gasp = json.loads(gasp_path.read_text())
        gasp['charge'] + 0.0
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if gasp.get('boot') == boot:
        return None  # Still this boot's: battery-eta restarted.
    gasp_path.unlink(missing_ok=True)
    if not isinstance(gasp.get('boot'), str) or number(gasp['charge']) is None:
        return None  # Damaged: nothing to learn.
    how, end = (endings(gasp['boot']) if endings else None) or (None, None)
    entry = judge(gasp, how, end)
    if endings is not None and how is None:
        entry = dict(entry or {'boot': gasp['boot'], 't': number(gasp.get('t'))}, gasp=gasp, unconfirmed=True)
    if entry is not None:
        remember([entry], deaths_path)
    return entry


def fit(minutes, known=None, previous=None, running=None, endings=None):
    """Every battery-eta model from `minutes`; `known` deaths default to deaths.json
    (read only). With under MIN_ROWS battery minutes (a month on AC, a store
    migration underway) the draw models stay as `previous` had them."""
    params = dict(DEFAULTS)
    battery = [m for m in minutes if m['st'] == 'D' and m.get('bat', 0) > 0 and m['dt'] >= 40]
    if previous and len(battery) < MIN_ROWS:
        params.update({k: previous[k] for k in DRAW_KEYS if k in previous})
    else:
        if (ou := fit_ou(battery)) is not None:
            params.update(ou)
            params['scale'] = fit_scale(battery, params)
        params['volts'] = fit_voltage(battery)
        params['kappa'] = fit_kappa(battery)
    params['charge_curve'], params['charge_sigma'] = fit_charging(minutes)
    if params['charge_curve'] is None and previous and previous.get('charge_curve'):
        params['charge_curve'], params['charge_sigma'] = previous['charge_curve'], previous['charge_sigma']
    params.update(fit_cutoff(minutes, load_gasps() if known is None else known,
                             running or power.boot_id(), endings))
    params['fitted'] = time.time()
    return params


MODELS = report.LOG / 'models.json'
# 4: per-horizon σ factors, the level's variance by run, the Jensen centre, 'typical'.
# 5: report.fit_rows' attribution: profile idle clocks, the active clock, setting columns, data health.
MODELS_VERSION = 5
WINDOW_DAYS = 30


def train(minutes, events, now=None, known=None, previous=None, running=None, endings=None):
    """Every model, from one window of history. This is the only place models
    are trained: the collector's battery-use breakdown and the battery panel
    read the result from models.json instead of fitting their own. The
    attribution is report.fit_rows' flat dict (with its data health), which
    keeps `previous`'s when there are too few battery rows, if that was trained
    by this MODELS_VERSION (an older one's terms meant other things); 'typical'
    is the mean battery draw now (typical_draw: the ETA's local level), and
    'typical_power' the window's mean draw at your settings, the one every gain
    per full charge is figured at (here, in recommend.json and in the panel).
    `minutes` load without apps: nothing here reads them."""
    rows = report.battery_rows(minutes)
    params = fit(minutes, known, previous, running, endings)
    params['sleep'] = report.sleep_model(report.sleep_drain(events, minutes))
    last = (previous or {}).get('attribution')
    if not isinstance(last, dict) or previous.get('version', MODELS_VERSION) != MODELS_VERSION:
        last = None
    params['attribution'] = {name: float(value)
                             for name, value in report.fit_rows(rows, previous=last, minutes=minutes).items()}
    gains = report.typical_power(rows, params['attribution']) if rows else None
    params.update(fitted=now or time.time(), window_days=WINDOW_DAYS, battery_minutes=len(rows),
                  typical=typical_draw(params),
                  typical_power=float(gains) if gains is not None and math.isfinite(gains) and gains > 0 else None)
    return params


def save_models(params, path=None):
    path = path or MODELS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'version': MODELS_VERSION, **params}))
    temporary.replace(path)


def load_models(path=None, max_age=3600, version=MODELS_VERSION):
    """The shared models if trained within `max_age` seconds, else None (any version with `version=None`)."""
    try:
        data = json.loads((path or MODELS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or (version is not None and data.get('version') != version):
        return None
    return data if time.time() - data.get('fitted', 0) <= max_age else None


def db_ready(db=None):
    """The power database is at this build's layout, its minutes recomputed after
    any migration (store.minutes_ready): before that a refit would read
    half-migrated minutes."""
    try:
        return bool(store.minutes_ready(db or store.DB))
    except Exception:  # noqa: BLE001 - not ready is the safe answer
        return False


# --- Forecasts --------------------------------------------------------------------

def usable_energy(charge_ah, full_ah, params):
    """Wh left above the cutoff (0 at or below it, or with no full charge known)."""
    if not full_ah or full_ah <= 0:
        return 0.0
    floor = remaining_energy(min(params.get('cutoff') or 0.0, charge_ah) / full_ah, full_ah, params['volts'])
    return max(0.0, remaining_energy(charge_ah / full_ah, full_ah, params['volts']) - floor)


def remaining_energy(fraction, full_ah, volts):
    """Wh left above empty: ∫ V(q) dq from 0 to the current charge fraction."""
    if not volts:
        return fraction * full_ah * 15.4
    pct = max(0.0, fraction * 100)
    whole = int(min(pct, 100))
    energy = sum(volts[:whole]) / 100 * full_ah
    if whole < 100:
        energy += (pct - whole) / 100 * full_ah * volts[whole]
    return energy


def ou_terms(x):
    """For a horizon of x time constants: the share of today's deviation the horizon
    mean keeps, the mean of its square, and the horizon mean's variance (in units of
    the variance); series below x = 0.01, where the closed forms cancel to noise."""
    if x < 0.01:
        return (1 - x / 2 + x * x / 6 - x ** 3 / 24, 1 - x + 2 * x * x / 3 - x ** 3 / 3,
                2 * x / 3 - x * x / 2 + 7 * x ** 3 / 30 - x ** 4 / 12)
    e = math.exp(-x)
    return (1 - e) / x, (1 - e * e) / (2 * x), max(0.0, (2 * x - 3 + 4 * e - e * e) / x ** 2)


def horizon_scale(params, minutes):
    """fit_scale's factor at `minutes`, interpolated in log minutes (1 without one)."""
    scale = params.get('scale')
    if not scale:
        return 1.0
    at, factor = scale['minutes'], scale['factor']
    if minutes <= at[0]:
        return factor[0]
    if minutes >= at[-1]:
        return factor[-1]
    i = bisect.bisect_right(at, minutes)
    share = math.log(minutes / at[i - 1]) / math.log(at[i] / at[i - 1])
    return factor[i - 1] + share * (factor[i] - factor[i - 1])


def draw_lognormal(params, recent, minutes):
    """(μ, σ) of log average draw over the next `minutes`: μ the median.

    The deviation from the level decays (carry). Each minute's draw is log-normal
    around that path, so the horizon's average sits above it by half the variance
    the horizon hasn't averaged away, and by half the spread of the decaying path
    itself: log(recent) at once, level + var/2 (the mean draw) over long horizons,
    within 0.2% of a simulated OU's median between. σ: the horizon average's
    variance plus the level's (the more of the horizon reverts to the level, the
    more its error counts), times the horizon's calibration factor.
    """
    tau, var, level = params['tau'], params['var'], params['level']
    carry, square, spread = ou_terms(max(minutes, 1e-3) / tau)
    deviation = math.log(max(recent, 0.1)) - level
    mu = (level + deviation * carry + var * max(0.0, 1 - square - spread) / 2
          + deviation ** 2 * max(0.0, square - carry ** 2) / 2)
    level_var = params.get('level_var')
    if level_var is None:  # Models from before the level's variance was stored.
        level_var = var * 2 * tau / max(params.get('level_n') or 1, 1)
    sigma = math.sqrt(var * spread + (1 - carry) ** 2 * level_var) * horizon_scale(params, minutes)
    return max(mu, math.log(params['floor'])), max(sigma, 1e-6)


def typical_draw(params):
    """Battery watts with nothing recent to go on: the mean draw at the local level
    (its median, e^level, is ~4% lower)."""
    return math.exp(params['level'] + params['var'] / 2)


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
    horizon forecast that gives the median and interval. No energy left: empty now.
    """
    if energy <= 0:
        return [0.0], [0.0], [1.0]
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
    """Hours (median, low, high): solve T = energy / draw over T for each quantile (0 with nothing left)."""
    if energy <= 0:
        return 0.0, 0.0, 0.0
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


# Settings recommendations leave alone if experiment.py can't say which: its RISKY ones (as of 2026-10).
RISKY = ('aspm', 'apst', 'gpu-dpm')


def unrecommended():
    """Settings recommendations must leave as they are (experiment.unrecommended:
    everything outside the experiment pool). If that fails, the risky ones and
    every quarantined one still readable: never none."""
    try:
        import experiment  # Here: experiment imports eta (lazily) too.
    except Exception as error:  # noqa: BLE001
        print(f'battery-eta: experiment.py: {error!r}; leaving the risky settings alone', file=sys.stderr)
        return RISKY
    try:
        return tuple(experiment.unrecommended())
    except Exception as error:  # noqa: BLE001
        print(f'battery-eta: experiment.unrecommended: {error!r}; leaving the risky and quarantined settings alone',
              file=sys.stderr)
    exclude = set(getattr(experiment, 'RISKY', RISKY))
    try:
        exclude |= set(experiment.quarantined())
    except Exception:  # noqa: BLE001,S110 - the risky ones at least
        pass
    return tuple(sorted(exclude))


def reserve_wh(params=None, sys_root=Path('/sys')):
    """Wh below the learned cutoff, which no charge ever delivers (0.0 until a
    death has taught it): ∫ V(q) dq up to the cutoff on the loaded-voltage curve,
    as usable_energy leaves it out. Cheap (the panel asks): `params` default to
    models.json's (any version or age), the cutoff to deaths.json's without one;
    the full charge only places the cutoff on the curve (taken as 2% without it)."""
    if params is None:
        params = load_models(max_age=float('inf'), version=None) or {}
    cutoff = number(params.get('cutoff'))
    if not cutoff or not params.get('deaths'):
        cutoff = fit_cutoff([], load_gasps())['cutoff']
    if not cutoff or cutoff <= 0:
        return 0.0
    volts = params.get('volts')
    if not (isinstance(volts, list) and len(volts) == 101 and all(number(v) for v in volts)):
        volts = None
    battery = find_battery(sys_root / 'class/power_supply')
    full = (report_number(battery / 'charge_full') or 0.0) / 1e6 if battery else 0.0
    if full <= cutoff:
        full = 50 * cutoff
    return remaining_energy(cutoff / full, full, volts)


def write_recommendations(minutes, events, path=None, params=None, exclude=None):
    """Each goal's best measured combination (report.recommendations) for the
    battery menu and `power-experiment goal --apply`: recommend.json. The draw
    it is figured at is the typical one at your settings (report.typical_power)
    by `params`' attribution, the models just trained (else models.json's,
    else fitted here); the energy a full charge delivers above the cutoff; and
    settings outside the experiment pool stay as they are (`exclude`, by
    default unrecommended()). `minutes` may come without apps."""
    rows = report.battery_rows(minutes)
    if params is None:
        params = load_models(max_age=float('inf'), version=MODELS_VERSION) or {}
    attribution = params.get('attribution') or report.fit_rows(rows, minutes=minutes)
    power = report.typical_power(rows, attribution)
    energy = report.full_energy(minutes, reserve_wh=reserve_wh(params))
    levers = report.lever_effects(minutes, energy, power, replicates=100)
    exclude = unrecommended() if exclude is None else tuple(exclude)
    recs = report.recommendations(levers, report.qol_model(minutes, events), energy, power, exclude=exclude)
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
    """The 2 s loop. Paths and clocks are injectable (tests never touch the real ones):
    `clock` stamps what is stored, `monotonic` (boot time: no jumps, counts suspend)
    times trends and windows."""

    def __init__(self, sys_root=Path('/sys'), proc=Path('/proc'), gasp=None, notify=None, poweroff=None,
                 deaths=None, models=None, db=None, runtime=None, endings=None, clock=time.time, monotonic=boottime):
        self.supplies = sys_root / 'class/power_supply'
        self.battery = find_battery(self.supplies)
        self.samples = collections.deque()
        self.boot, self.gasp, self.deaths = power.boot_id(proc), gasp or GASP, deaths or DEATHS
        self.models, self.db, self.runtime, self.endings = models or MODELS, db, runtime or RUNTIME, endings
        self.clock, self.monotonic = clock, monotonic
        self.notify, self.warned = notify or send_notification, set()
        self.poweroff, self.due, self.powering_off, self.retry_at = poweroff or power_off, None, False, 0.0
        self.last, self.low, self.complaints = None, False, set()
        self.mains_since = None  # Boot time mains came online while the battery went on discharging.
        self.fitting, self.next_fit = None, 0.0
        self.params = dict(DEFAULTS)
        self.load_cutoff()

    def load_cutoff(self):
        """The cutoff from deaths.json, whatever models.json says (missing, old, or a
        refit failing): without it the power-off would be disarmed. A deaths.json
        that can't be read leaves the cutoff as it was; it never stops the loop."""
        try:
            cutoff = fit_cutoff([], load_gasps(self.deaths))
        except Exception as error:  # noqa: BLE001 - the warnings and power-off must run regardless
            self.complain('cutoff from deaths.json', error)
            return
        if cutoff['deaths']:
            self.params = {**self.params, **cutoff}

    def complain(self, what, error):
        """stderr once per kind of failure, not every 2 s."""
        if (what, str(error)) not in self.complaints:
            self.complaints.add((what, str(error)))
            print(f'battery-eta: {what} failed: {error!r}', file=sys.stderr)

    # Training, in the background.

    def maybe_refit(self):
        """Start a refit when one is due, unless one is running or the battery is low
        (known only once an estimate has read it)."""
        if self.last is None or self.low:
            return
        if (self.fitting is None or not self.fitting.is_alive()) and self.monotonic() >= self.next_fit:
            self.fitting = threading.Thread(target=self.refit, name='refit', daemon=True)
            self.fitting.start()

    def refit(self):
        """Every model from the last WINDOW_DAYS, swapped in whole. A database at
        another layout (mid-migration) or a failure tries again in RETRY s."""
        try:
            if not db_ready(self.db):
                print('battery-eta: the power database is not at this layout yet; refitting in a minute',
                      file=sys.stderr)
                self.next_fit = self.monotonic() + RETRY
                return False
            now = self.clock()
            minutes, events = report.load_range(now - WINDOW_DAYS * 86400, now + 60, self.db, apps=False)
            # Deaths in these minutes join deaths.json, so the cutoff outlives the window.
            known = remember_deaths(minutes, self.boot, self.deaths, self.endings)
            params = train(minutes, events, now=now, known=known, previous=self.params, running=self.boot,
                           endings=self.endings)
            save_models(params, self.models)
        except Exception as error:  # noqa: BLE001 - try again soon; the estimate goes on with the last models
            print(f'battery-eta: refit failed: {error!r}', file=sys.stderr)
            self.next_fit = self.monotonic() + RETRY
            return False
        self.params, self.next_fit = params, self.monotonic() + REFIT
        try:
            write_recommendations(minutes, events, self.runtime / 'recommend.json', params=params)
        except Exception as error:  # noqa: BLE001 - the menu's lines must never stop the estimate
            print(f'battery-eta: recommendations failed: {error}', file=sys.stderr)
        return True

    def seed(self, minutes):
        """The last minutes on battery from the power log, so a restart isn't blind."""
        now, mono = self.clock(), self.monotonic()
        for m in minutes:
            t = m['t'] + 60
            if m['st'] == 'D' and m.get('bat') and 0 <= now - t < 900:
                charge = m['charge'] / 1e6 if m.get('charge') is not None and m.get('unit') != 'uWh' else None
                self.samples.append((mono - (now - t), m['bat'], 'Discharging', charge))

    # Readings.

    def kind(self, status, mains):
        """'discharging', 'charging' or 'plugged'. Only Not charging, Full or mains
        online are plugged: any other status (Unknown) on battery is discharging."""
        if status in ('Discharging', 'Charging'):
            return status.lower()
        return 'plugged' if status in PLUGGED or mains else 'discharging'

    def sample(self, mono):
        self.battery = b = self.battery or find_battery(self.supplies)
        if b is None:
            return None
        status = read(b / 'status') or 'Unknown'
        try:
            current, volts = int(read(b / 'current_now')) / 1e6, int(read(b / 'voltage_now')) / 1e6
            charge, full = int(read(b / 'charge_now')) / 1e6, int(read(b / 'charge_full')) / 1e6
        except (TypeError, ValueError):
            if not b.exists():
                self.battery = None  # Gone: look again next time.
            return None
        mains = bool(power.mains_online(self.supplies))
        kind = self.kind(status, mains)
        if kind == 'discharging' and mains:
            self.mains_since = mono if self.mains_since is None else self.mains_since
        else:
            self.mains_since = None
        capacity = report_number(b / 'capacity')
        if full <= 0:  # No full charge to divide by: the gauge's own percent.
            full, pct = None, capacity if capacity is not None else 0.0
        else:
            pct = 100 * charge / full
        if kind != 'plugged':
            self.samples.append((mono, current * volts, 'Discharging' if kind == 'discharging' else status, charge))
        while self.samples and self.samples[0][0] < mono - 900:
            self.samples.popleft()
        return {'status': status, 'kind': kind, 'watts': current * volts, 'charge': charge, 'full': full,
                'volts': volts, 'pct': pct, 'limit': report_number(b / 'charge_control_end_threshold'),
                'capacity': capacity, 'mains': mains}

    def weak_charger(self, mono):
        """Mains online for WEAK_SECONDS and the battery still discharging: the charger can't keep up."""
        return self.mains_since is not None and mono - self.mains_since >= WEAK_SECONDS

    def plugged_now(self, mono):
        """A fresh look at the charger, just before powering off. Plugged: the status
        has left Discharging (Charging, Not charging, Full), or mains is online and
        hasn't been for WEAK_SECONDS (a plug-in the status hasn't caught up with).
        Mains online longer with the battery still discharging is a charger too
        weak to keep up, and doesn't stop the power-off."""
        status = read(self.battery / 'status') if self.battery else None
        if status in ('Charging', *PLUGGED):
            return True
        return bool(power.mains_online(self.supplies)) and not self.weak_charger(mono)

    def recent(self, status, seconds, mono):
        values = [w for t, w, s, _ in self.samples if s == status and t >= mono - seconds]
        return sum(values) / len(values) if values else None

    def gauge_trend(self, mono, seconds=FINAL_SECONDS, span=60, after=None):
        """(charge now, Ah per second falling) from the gauge's own readings over
        the last `seconds` on battery (and since `after`): it lowers its estimate
        faster under heavy draw, which the current alone misses. None with under
        `span` s of them."""
        start = max(mono - seconds, after if after is not None else -math.inf)
        points = [(t - mono, q) for t, _, s, q in self.samples
                  if s == 'Discharging' and q is not None and t >= start]
        if len(points) < 10 or points[-1][0] - points[0][0] < span:
            return None
        slope, level = np.polyfit(*np.array(points).T, 1)
        return (float(level), float(-slope)) if slope < 0 else None

    def charge_rate(self, full, mono):
        """Percent per minute over the last five minutes of charging (charge counter)."""
        points = [(t, q) for t, _, s, q in self.samples if s == 'Charging' and q is not None and t >= mono - 300]
        if not full or len(points) < 2 or points[-1][0] - points[0][0] < 60:
            return None
        (t0, q0), (t1, q1) = points[0], points[-1]
        return (q1 - q0) / full * 100 / ((t1 - t0) / 60)

    # The safety path: black box, warnings, power-off.

    def record_gasp(self, state, timing):
        """The black box: below LOW_BATTERY, the last reading on disk every 2 s, atomically
        (marked once battery-eta has started powering the laptop off: that isn't a death)."""
        gasp = {'t': self.clock(), 'boot': self.boot, 'charge': state['charge'], 'full': state['full'],
                'volts': state['volts'], 'watts': state['watts'], 'rate': timing['rate'] if timing else None,
                'seconds': timing['seconds'] if timing else None, 'poweroff': self.powering_off}
        try:
            write_atomic(self.gasp, json.dumps(gasp))
        except OSError as error:
            self.complain('last gasp', error)

    def clear_gasp(self):
        """Plugged in: not a death, and the warnings start over."""
        self.warned.clear()
        self.due, self.powering_off = None, False
        self.gasp.unlink(missing_ok=True)

    def warns_off(self):
        """Once a death has taught the cutoff, battery-eta powers off before it."""
        return self.params.get('deaths', 0) >= 1

    def warn(self, early, likely, weak=False):
        """A notification as the early edge of the end (`early` s away; `likely` its
        middle) comes within each of WARN_SECONDS (`weak`: on a charger that can't
        keep up, where plugging in is no answer)."""
        for limit in WARN_SECONDS:
            if early <= limit and limit not in self.warned:
                self.warned.update(w for w in WARN_SECONDS if w >= limit)
                at = time.strftime('%H:%M:%S', time.localtime(self.clock() + max(0.0, early)))
                when = f'In {countdown(early)} at the earliest, {countdown(likely)} likely'
                if self.warns_off():
                    self.notify(f'Powering off as soon as {at}',
                                f'{when}: {POWEROFF_SECONDS} s before the battery could cut out. '
                                + ("The charger can't keep up: use a stronger one." if weak else 'Plug in to stop it.'))
                else:
                    self.notify(f'Battery cuts out as soon as {at}', f'{when}. ' + (
                        "The charger can't keep up: save your work." if weak else 'Plug in, or save your work.'))
                return

    def maybe_power_off(self, state, timing, due, mono):
        """Power off cleanly once the early edge of the cutoff is POWEROFF_SECONDS
        away: apps close, the disks settle, the journal keeps its tail. Only on two
        readings within DUE_SECONDS that say so, as the gauge steps, and with the
        charge close enough to the cutoff that a wild trend can't (the time the
        lead takes at this rate, half again). Re-checks the charger first (plugged_now:
        on a charger too weak to keep up, only the gauge's fall since it came online
        can time it); if `systemctl poweroff` fails it says so and tries again
        POWEROFF_RETRY s on.

        The black box says 'powering off' before the call: logind starts the shutdown
        as it accepts, and stopping waybar.service (last night 0.1 s later) kills this
        process, so a mark written after could be lost, and the power-off then taken
        for a death without the journal. A failed call unmarks it."""
        params = self.params
        rate = timing['rate'] or state['watts'] * params['kappa'] / max(state['volts'], 1.0) / 3600
        margin = timing['seconds'] - timing['early']
        if not (self.warns_off() and state['pct'] < LOW_BATTERY and timing['early'] <= POWEROFF_SECONDS
                and state['charge'] - params['cutoff'] <= max(0.010, 1.5 * rate * (POWEROFF_SECONDS + margin))
                and (timing['source'] == 'gauge' or not state.get('mains'))):
            return
        self.due = mono
        if self.powering_off or due is None or mono - due > DUE_SECONDS or mono < self.retry_at:
            return
        if self.plugged_now(mono):
            return
        weak = " The charger can't keep up." if self.weak_charger(mono) else ''
        self.notify('Powering off', f'The battery could cut out in {countdown(timing["early"])}.{weak}')
        self.powering_off = True
        self.record_gasp(state, timing)
        if not self.poweroff():
            self.powering_off = False
            self.record_gasp(state, timing)
            self.retry_at = mono + POWEROFF_RETRY
            self.notify("Couldn't power off", f'Trying again in {POWEROFF_RETRY} s. Plug in, or shut down now.')

    def timing(self, params, forecast, final, steady):
        """When the battery cuts out, as {'seconds', 'early', 'late', 'rate', 'source'}
        (s from now; Ah/s falling, if the gauge says; 'gauge' or 'forecast'), or
        None: the forecast's interval; below LOW_BATTERY the gauge's slope over the
        last STEADY_SECONDS; and once either says FINAL_MINUTES, its slope over
        FINAL_SECONDS (the longer slope lags a draw that just rose: af7f85d1 went
        from 17 to 25 W 3 minutes from the end). ± where the cutoff falls (two
        spreads), and for the steadier slope a quarter of the time left (the
        gauge's fall steepens toward the end)."""
        def by_gauge(trend, share):
            charge, rate = trend
            seconds = max(0.0, (charge - params['cutoff']) / rate)
            pm = max(10.0, 2 * params['cutoff_spread'] / rate, share * seconds)
            return {'seconds': seconds, 'early': max(0.0, seconds - pm), 'late': seconds + pm, 'rate': rate,
                    'source': 'gauge'}
        best = None
        if steady:
            best = by_gauge(steady, 0.25)
        elif forecast:
            best = {'seconds': forecast['hours'] * 3600, 'early': forecast['low'] * 3600,
                    'late': forecast['high'] * 3600, 'rate': final[1] if final else None, 'source': 'forecast'}
        if final:
            quick = by_gauge(final, 0.0)
            if best is None or min(best['seconds'], quick['seconds']) < FINAL_MINUTES * 60:
                best = quick
        return best

    def forecast(self, state, params, mono):
        """The draw forecast's time to empty (hours) and its distribution."""
        energy = usable_energy(state['charge'], state['full'], params)
        # A short session borrows from the local level instead of trusting a few seconds.
        recent = self.recent('Discharging', 600, mono)
        n = sum(1 for t, _, s, _ in self.samples if s == 'Discharging' and t >= mono - 600)
        weight = min(1.0, n / 60)
        typical = typical_draw(params)
        draw = (recent or typical) * weight + typical * (1 - weight)
        median, low, high = time_to_empty(params, energy, draw)
        times, pdf, _ = empty_distribution(params, energy, draw, low, high)
        return {'hours': median, 'low': low, 'high': high, 'draw': draw, 'times': times, 'pdf': pdf}

    def estimate(self):
        now, mono = self.clock(), self.monotonic()
        if self.last is not None and mono - self.last > SUSPEND_GAP:
            self.samples.clear()  # A suspend (or a stall): readings before it don't belong to the trend.
            self.mains_since = None
        self.last = mono
        due, self.due = self.due, None  # Only a reading that qualifies again carries it on.
        state = self.sample(mono)
        if state is None:
            self.low = False
            return {'text': '--', 'class': ['unknown']}
        params = self.params
        result = {'status': state['status'], 'pct': state['pct'], 'watts': state['watts'], 'updated': now}
        try:
            energy_full = usable_energy(state['full'], state['full'], params)
            energy = usable_energy(state['charge'], state['full'], params)
            fill = round(100 * energy / energy_full) if energy_full else round(state['pct'])
            result.update(energy_pct=fill, energy_wh=energy)
            if (asleep := report.sleep_runtime([], energy, model=params.get('sleep'))) is not None:
                result['sleep'] = asleep  # How long a suspend from now would last.
        except Exception as error:  # noqa: BLE001
            self.complain('energy', error)
            energy, fill = None, round(state['pct'])
            result['energy_pct'] = fill
        classes = [f'fill{max(0, min(100, fill))}']
        self.low = state['kind'] == 'discharging' and state['pct'] < LOW_BATTERY
        if state['kind'] == 'discharging':
            # Mains online with the battery discharging: a plug-in the status hasn't followed yet
            # (held: no alarms, no power-off), then, after WEAK_SECONDS, a charger too weak to keep
            # up, timed by the gauge's fall since it came online.
            weak = self.weak_charger(mono)
            plugging = state['mains'] and not weak
            after = self.mains_since if weak else None
            final = self.gauge_trend(mono, after=after)
            steady = self.gauge_trend(mono, STEADY_SECONDS, STEADY_SPAN, after=after) if self.low else None
            forecast = None
            if energy is not None and state['full']:
                try:
                    forecast = self.forecast(state, params, mono)
                except Exception as error:  # noqa: BLE001 - the safety path below must still run
                    self.complain('forecast', error)
            timing = self.timing(params, forecast, final, steady)
            # The end the user sees: the power-off, once there is one, else the cutoff.
            end = None
            if timing:
                lead = POWEROFF_SECONDS if self.warns_off() else 0.0
                end = max(0.0, timing['early'] - lead) if self.warns_off() else timing['seconds']
                if not plugging:
                    self.warn(timing['early'] - lead, timing['seconds'] - lead, weak)
                self.maybe_power_off(state, timing, due, mono)
            if state['pct'] < LOW_BATTERY:
                self.record_gasp(state, timing)
            classes.append('discharging')
            watts = f"<span alpha='70%'>{state['watts']:.1f}W</span>"
            if weak:
                classes.append('weak-charger')
                result['charger'] = 'too weak'
                watts += " <span size='small' alpha='70%'>charger too weak</span>"
            if timing is None:
                text = f"{icon(HOURGLASS)} {state['pct']:.0f}% {watts}"  # Neither the forecast nor the gauge: just the charge.
            else:
                seconds, pm = timing['seconds'], (timing['late'] - timing['early']) / 2
                result.update(hours=seconds / 3600, low=timing['early'] / 3600, high=timing['late'] / 3600)
                if forecast:
                    result['draw'] = forecast['draw']
                    result['distribution'] = {'kind': 'empty', 't': [round(t, 4) for t in forecast['times']],
                                              'p': [round(v, 5) for v in forecast['pdf']]}
                if seconds < 1800 or fill <= 10:
                    classes.append('low')
                if seconds < COUNTDOWN_MINUTES * 60:
                    result.update(cutout=now + seconds, cutout_pm=pm)
                    if self.warns_off():
                        result['poweroff'] = now + end
                    if seconds < FINAL_MINUTES * 60:
                        classes.append('critical')
                    glyph = POWER if self.warns_off() else HOURGLASS
                    text = f"{icon(glyph)} {countdown(end)} <span size='small' alpha='70%'>±{pm:.0f}s</span> {watts}"
                else:
                    text = f"{icon(HOURGLASS)} {clock(seconds / 3600)} {plus_minus(timing['early'] / 3600, timing['late'] / 3600)} {watts}"
        elif state['kind'] == 'charging':
            self.clear_gasp()
            classes.append('charging')
            try:
                text = self.charging(state, params, energy, energy_full, result, mono)
            except Exception as error:  # noqa: BLE001
                self.complain('charging estimate', error)
                text = f"{icon(BOLT)} {state['pct']:.0f}% <span size='small' alpha='70%'>{abs(state['watts']):.0f}W</span>"
        else:
            self.clear_gasp()
            classes.append('plugged')
            limit = state['limit'] or 100
            held = state['pct'] >= limit - 1.5
            text = f"{icon(PLUG)} " + ('held at limit' if held else 'on AC')
        result['text'], result['classes'] = text, classes
        return {'text': text, 'class': classes, 'percentage': fill, 'detail': result}

    def publish(self, detail):
        """eta.json for the battery panel. A failure (no runtime directory, a full
        disk) is reported, never raised: the label and the power-off go on."""
        try:
            params = self.params
            shown = {k: v for k, v in params.items() if k in ('mean', 'tau', 'kappa', 'charge_sigma')}
            self.runtime.mkdir(parents=True, exist_ok=True)
            temporary = self.runtime / 'eta.tmp'
            temporary.write_text(json.dumps(dict(detail, params=dict(shown, typical=typical_draw(params)))))
            temporary.replace(self.runtime / 'eta.json')
        except Exception as error:  # noqa: BLE001
            self.complain('eta.json', error)

    def charging(self, state, params, energy, energy_full, result, mono):
        target = state['limit'] or 100
        rate = self.charge_rate(state['full'], mono)
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
        # Both ranges are wide (≈±20% / ±30%), so the label shows ~ and leaves them to the menu.
        return (f"{icon(BOLT)} {APPROX}{clock(banked)} <span size='small' alpha='70%'>full ~{clock(median / 60)}</span> "
                f"<span size='small' alpha='70%'>{abs(state['watts']):.0f}W</span>")


def power_off(timeout=10):
    """`systemctl poweroff`: True once logind has accepted it."""
    try:
        done = subprocess.run(['systemctl', 'poweroff'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        print(f'battery-eta: systemctl poweroff: {error}', file=sys.stderr)
        return False
    if done.returncode:
        print(f'battery-eta: systemctl poweroff: {done.stderr.strip() or done.returncode}', file=sys.stderr)
    return done.returncode == 0


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
    endings = Endings()
    live = Live(endings=endings)
    try:  # A damaged black box or deaths.json must never keep the loop below from running.
        last = harvest_gasp(live.boot, endings=endings)
        if last is not None and (last.get('cut') is not None or last.get('poweroff')):
            print(f"battery-eta: the last boot {'powered off' if last.get('poweroff') else 'died'} at "
                  f"{last['charge'] * 1000:.0f} mAh, {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last['t']))}",
                  file=sys.stderr)
    except Exception as error:  # noqa: BLE001
        live.complain('last gasp', error)
    # Start from the last trained models (any version) so the label appears at once;
    # the cutoff comes from deaths.json whatever they say. Training follows in the background.
    if (saved := load_models(max_age=float('inf'), version=None)) is not None:
        live.params = {**DEFAULTS, **saved}
    live.load_cutoff()
    try:
        live.seed(report.load_range(time.time() - 1200, time.time() + 60, apps=False)[0])
    except Exception as error:  # noqa: BLE001
        print(f'battery-eta: seeding from the log failed: {error!r}', file=sys.stderr)
    while True:
        try:
            output = live.estimate()
        except Exception as error:  # noqa: BLE001 - a label, whatever happened
            live.complain('estimate', error)
            output = {'text': '--', 'class': ['unknown']}
        detail = output.pop('detail', None)
        print(json.dumps(output), flush=True)
        if detail:
            live.publish(detail)
        live.maybe_refit()  # After the estimate: it knows whether the battery is low.
        time.sleep(2)


if __name__ == '__main__':
    main()
