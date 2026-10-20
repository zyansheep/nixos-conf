"""Battery history analysis for the battery panel.

Per-minute rows from the power database (store.py, written by the collector),
then:

* battery-only models: chip W ~ floor + clock (mean GHz from the profile's
  idle clock, or the active cores' clock) + load (busy cores × GHz²) + GPU +
  video, and battery W ~ base + k·chip + backlight + radio/storage/USB/audio/
  keyboard, both with a column per setting (experiment minutes count, at your
  settings), fitted by non-negative least squares on one-minute means (the
  battery reading lags chip power by ~10 s);
* per-minute attribution into stable groups for the stacked timeline;
* what-if runtime gains: model-based (marginal effect of removing an app's
  activity or dimming) with bootstrap intervals over whole days, and
  experiment-based (paired A/B blocks from power-experiment; every setting
  together in lever_effects, and the best combination, best_config);
* sleep drain per suspend from the log's gap events.
"""
import bisect
import collections
import datetime
import itertools
import math
import os
import re
import sqlite3
from pathlib import Path

# Before numpy: these are small matrices, and BLAS threads only contend for them
# (a 226×76 least squares took 27 ms with 12 threads, 4 ms with one).
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

import numpy as np  # noqa: E402
from scipy.optimize import minimize, nnls  # noqa: E402
from scipy.special import expit, ndtr, stdtrit  # noqa: E402

import power  # noqa: E402
import store  # noqa: E402

LOG = store.STATE / 'power'  # The JSONL archive, models.json and index.json.

# Stable groups so a color always means the same thing across periods.
GROUPS = [
    ('Browser', ('Floorp', 'Firefox', 'Chromium', 'Chrome', 'Brave', 'Zen')),
    ('Builds & EDA', ('openroad', 'yosys', 'strands', 'rustc', 'cargo', 'cc1', 'C compiler', 'C++ compiler',
                      'ld', 'make', 'nix', 'klayout', 'verilator', 'iverilog', 'gcc', 'clang', 'cmake', 'ninja')),
    ('Coding tools', ('T3 Code', 'VSCodium', 'VS Code', 'Electron', 'claude', 'codex', 'node', 'Cursor', 'kimi')),
    ('Chat & media', ('Vesktop', 'Signal', 'signal', 'Obsidian', 'PipeWire', 'WirePlumber', 'mpv', 'spotify')),
    ('Desktop & system', ('Kernel', 'Niri', 'Waybar', 'Notifications', 'Wi-Fi panel', 'System monitor', 'Xwayland',
                          'fcitx5', 'vicinae', 'systemd', 'dbus', 'NetworkManager', 'iwd', 'tailscale', 'swayidle',
                          'aw-', 'xdg-', 'swaylock', 'awww', 'wl-')),
]
APP_GROUPS = [name for name, _ in GROUPS] + ['Other apps']
IDLE = power.IDLE
DEVICES = (('Display', 'bl'), ('Wi-Fi', 'wifi'), ('Storage', 'disk'), ('USB devices', 'usb'),
           ('Audio', 'audio'), ('Keyboard backlight', 'kbd'))
CHIP_TERMS = ('floor', 'load', 'clock', 'gpu', 'video', 'clock_active')
BATTERY_TERMS = ('base', 'chip', 'bl', 'wifi', 'disk', 'usb', 'audio', 'kbd')
PRIOR = power.PRIOR  # Shared with the collector's attribution.
IDLE_PERCENTILE = 5  # The idle clock: minutes rarely run slower (~1.0 GHz, where ~1 core is busy).


def display_name(app):
    """Undo /proc/PID/comm truncation artifacts like 'openroad-wrapp'."""
    return re.sub(r'-w(?:r(?:a(?:p(?:p(?:e(?:d)?)?)?)?)?)?$|\.bin$', '', app)


def group_of(app):
    for group, prefixes in GROUPS:
        if app.startswith(prefixes):
            return group
    return 'Other apps'


def load_range(start, end, db=None, apps=True):
    """Per-minute rows and events with start <= t < end, from the power database
    (without each minute's 'apps' if not `apps`). Experiment minutes also get
    their clean parts (add_clean_parts)."""
    db = db or store.DB
    try:
        minutes, events = store.load_range(start, end, db, apps=apps)
    except TypeError:  # A store without the apps switch.
        minutes, events = store.load_range(start, end, db)
    if minutes:
        add_clean_parts(minutes, db)
    return minutes, events


def add_clean_parts(minutes, db):
    """Blocks don't start on the minute, and one washout record makes its whole
    minute washout (store.Minute): that threw away half of the experiment
    minutes, where only 30% of records are washout. So each experiment minute
    gets `clean`: its battery records outside washout as minute-like dicts, one
    per block (read-only from `records`), which experiment_blocks and
    lever_blocks use in its place."""
    try:
        conn = sqlite3.connect(Path(db).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    except sqlite3.OperationalError:
        return
    conn.row_factory = sqlite3.Row
    parts = {}
    try:
        for row in conn.execute("SELECT * FROM records WHERE t1 >= ? AND t1 < ? AND exp_run IS NOT NULL "
                                "AND exp_washout = 0 AND status = 'Discharging' ORDER BY t1",
                                (minutes[0]['t'], minutes[-1]['t'] + 60)):
            row = dict(row)
            key = (store.minute_of(row['t1']), row['exp_run'], row['exp_block'])
            if key not in parts:
                parts[key] = store.Minute(key[0])
            parts[key].add(row, ())
    except sqlite3.Error:
        return  # No records table (yet): minutes alone.
    finally:
        conn.close()
    clean = collections.defaultdict(list)
    for (t, *_), part in parts.items():
        if part.dt > 0:
            clean[t].append(dict(part.row(), part=True))
    for m in minutes:
        if m.get('exp') and m['t'] in clean:
            m['clean'] = clean[m['t']]


# --- Models -------------------------------------------------------------------

def battery_rows(minutes, include_experiments=True):
    """Minutes the battery models can use: on battery, with chip power, load and
    clock measured. Experiment minutes count except their washout: the models
    adjust for settings (fit_rows), and since 2026-10-12 nearly every battery
    minute has been an experiment's, so leaving them out froze every model on
    the days before. `include_experiments=False`: only minutes outside them.
    A minute that was partly on AC (`purity`, the share of it in its majority
    status, under PURE) is left out; one without purity (not yet recomputed) stays."""
    return [m for m in minutes if m['st'] == 'D' and m.get('bat', 0) > 0 and m.get('soc') is not None
            and m.get('load') is not None and m.get('ghz') is not None and m['dt'] >= 40
            and (m.get('purity') is None or m['purity'] >= PURE)
            and not (m.get('exp') and (m['exp'][4] or not include_experiments))
            and m['bat'] - m['soc'] > -2]


PURE = 0.9


MIN_ROWS = 20  # Battery minutes a fit needs; with fewer, fit_rows keeps the last model.
IDLE_MINUTES = 30  # Minutes of a profile (on battery, or on AC) to learn its own idle clock.
STATE_MINUTES = 30  # Battery minutes in each state for a setting to get its own model column.
ACTIVE_MINUTES = 120  # Battery minutes with the active cores' clock (store layout 9) before the model uses it.
RAIL_MINUTES = 30  # Battery minutes with the chip's rails (store layout 10) before the idle chip is split by them.


def profile_of(m):
    return (m.get('set') or {}).get('profile')


def idle_of(model, profile, ac=False):
    """The idle clock (GHz) of `profile`: on AC its own if learned, then its
    battery one, then the pooled one (power.clock_split looks it up the same way)."""
    pooled = model.get('idle_ghz', PRIOR['idle_ghz'])
    battery = model.get(f'idle_ghz:{profile}', pooled)
    return model.get(f'idle_ghz:{profile}:ac', battery) if ac else battery


def clock_of(m, model):
    """The minute's mean clock, measured from its profile's idle clock and put
    on the pooled idle clock's scale. Idle cores report a placeholder clock the
    profile sets (scaling_min_freq: 0.41 GHz power-saver, 1.12 GHz balanced), so
    the raw mean clock of an idle machine says which profile ran rather than
    what it cost: fitted on 91% power-saver minutes, the raw clock over-predicted
    balanced ones by 1.6 W. An unknown profile keeps the raw clock."""
    return m['ghz'] - idle_of(model, profile_of(m), m['st'] != 'D') + model.get('idle_ghz', PRIOR['idle_ghz'])


def active_clock(m, model):
    """The minute's active-core clock (GHz, `ghz_active`: idle cores' placeholder
    left out) if the model uses it — fit_rows learned 'idle_ghz_active' — else None."""
    return m.get('ghz_active') if 'idle_ghz_active' in model else None


def clock_columns(m, model):
    """The chip model's 'clock' and 'clock_active' columns for a minute, as
    power.clock_split reads them: with an active-core clock, the pooled idle
    clock (its baseline) and the active clock from its own idle; else the mean
    clock (clock_of) and nothing. The two kinds of minutes share the baseline."""
    active = active_clock(m, model)
    if active is not None:
        return model['idle_ghz'], active - model['idle_ghz_active']
    return (clock_of(m, model) if model else m['ghz']), 0.0


def idle_clocks(minutes):
    """'idle_ghz:<profile>' from battery minutes and 'idle_ghz:<profile>:ac' from
    AC ones: the IDLE_PERCENTILE-th percentile of each one's minute clocks, if it
    has IDLE_MINUTES (the placeholder differs on AC: balanced idles at 1.9 GHz, 1.5 on battery)."""
    clocks = collections.defaultdict(list)
    for m in minutes:
        if m.get('ghz') and m.get('dt', 0) >= 40 and profile_of(m):
            clocks[f"idle_ghz:{profile_of(m)}{'' if m['st'] == 'D' else ':ac'}"].append(m['ghz'])
    return {key: float(np.percentile(v, IDLE_PERCENTILE)) for key, v in clocks.items() if len(v) >= IDLE_MINUTES}


def model_levers(model):
    """The settings `model` has columns for ('own:<setting>' keys), in SETTING order."""
    return [n for n in SETTING if f'own:{n}' in model]


def model_top(model):
    return {key: model.get(f'top:{key}') for key in ('hz', 'mhz', 'cpu_mhz')}


def minute_states(m, top):
    """The minute's settings as saving states (lever_states: an experiment's values win)."""
    exp = m.get('exp')
    levers = [n for n in str(exp[1]).split('+') if n in SETTING] if exp else []
    return lever_states(m.get('set') or {}, top, experiment_values(exp, levers) if levers else None)


def design(rows, model=None):
    """Model matrices and targets: chip (CHIP_TERMS: floor, load, clock, GPU,
    video, active clock) and battery (base, chip, devices). With a `model`
    (fit_rows' idle clocks and settings) the clocks are clock_columns', and
    both get a column per setting it has: saving state minus yours, so the
    intercepts are at your settings and a setting's effect (an experiment's)
    stays out of the parts' coefficients."""
    model, n = model or {}, len(rows)
    levers, top = model_levers(model), model_top(model)
    own = np.array([model[f'own:{name}'] for name in levers])
    states = np.array([[float(s[name]) for name in levers] for s in (minute_states(m, top) for m in rows)]
                      if levers else []).reshape(n, len(levers)) - own
    chip = np.array([[1.0, m['load'], clock, m.get('gpu', 0), m.get('video', 0), active]
                     for m in rows for clock, active in (clock_columns(m, model),)]).reshape(n, len(CHIP_TERMS))
    battery = np.array([[1.0, m['soc'], m.get('bl') or 0, m.get('wifi', 0), m.get('disk', 0), m.get('usb', 0),
                         m.get('audio', 0), m.get('kbd') or 0] for m in rows]).reshape(n, len(BATTERY_TERMS))
    return (np.hstack([chip, states]), np.array([m['soc'] for m in rows]),
            np.hstack([battery, states]), np.array([m['bat'] for m in rows]))


def nnls_normal(a, b):
    """argmin over x >= 0 of xᵀax − 2bᵀx: least squares from its normal
    equations, as NNLS on the Cholesky factor (a = rᵀr), so a bootstrap
    resample only reweights the sums instead of stacking rows."""
    r = np.linalg.cholesky(a).T
    return nnls(r, np.linalg.solve(r.T, b))[0]


def gram(x, y, w=None):
    """The normal equations' sums, xᵀWx and xᵀWy."""
    xw = x if w is None else x * w[:, None]
    return xw.T @ x, xw.T @ y


def solve_gram(g, gy, names, levers=(), ridge=5.0):
    """fit's least squares from its sums (gram): coefficients of `names`, and of
    the `levers` columns after them, either sign (each the difference of two
    non-negative parts)."""
    k, n = len(names), len(levers)
    index = np.r_[np.arange(k + n), np.arange(k, k + n)].astype(int)
    sign = np.r_[np.ones(k + n), -np.ones(n)]
    prior = np.array([PRIOR[name] for name in names] + [0.0] * (2 * n))
    penalty = np.full(len(prior), float(ridge))
    penalty[0] = 0.01  # Intercepts: barely shrunk.
    coef = nnls_normal(g[np.ix_(index, index)] * np.outer(sign, sign) + np.diag(penalty),
                       gy[index] * sign + penalty * prior)
    return {name: float(c) for name, c in zip(names, coef[:k])}, coef[k:k + n] - coef[k + n:]


def fit(chip_x, chip_y, battery_x, battery_y, ridge=5.0, levers=(), weights=None, sums=None):
    """Non-negative least squares, lightly shrunk toward PRIOR so coefficients of
    features that never varied stay at their prior instead of drifting. Columns
    past the named terms are `levers`' settings (design): either sign, shrunk
    toward 0, as 'soc:<setting>' and 'bat:<setting>'. `weights`: how often each
    row counts; `sums`: both models' gram() sums instead (a resample)."""
    w = None if weights is None else np.asarray(weights, float)
    chip, battery = sums or (gram(chip_x, chip_y, w), gram(battery_x, battery_y, w))
    model = {}
    if len(chip_y) >= MIN_ROWS:
        terms, soc = solve_gram(*chip, CHIP_TERMS, levers, ridge)
        model.update(terms)
        terms, bat = solve_gram(*battery, BATTERY_TERMS, levers, ridge)
        model.update(terms)
        model.update({f'soc:{n}': float(v) for n, v in zip(levers, soc)})
        model.update({f'bat:{n}': float(v) for n, v in zip(levers, bat)})
        model['idle_ghz'] = float(np.percentile(chip_x[:, 2], IDLE_PERCENTILE))
    else:
        model.update(PRIOR)
    return model


def fit_rows(rows, previous=None, minutes=None):
    """The attribution models (models.json's 'attribution') from battery_rows,
    as a flat {name: float}:

    * CHIP_TERMS and BATTERY_TERMS; 'idle_ghz', the pooled idle clock, and each
      profile's ('idle_ghz:<profile>', and '…:ac' from AC minutes in `minutes`);
      'idle_ghz_active', the active cores' idle clock, once ACTIVE_MINUTES rows
      have it (clock_columns: from then on their clock is that one);
    * per setting with STATE_MINUTES in both states: 'soc:<s>' and 'bat:<s>',
      the chip and the other battery W of its saving state, 'own:<s>' yours (1
      or 0; the share of minutes when no minute outside experiments says), and
      'top:hz|mhz|cpu_mhz', what lever_states compares with;
    * data health: 'rows' used, 'experiment_rows' and 'active_rows' (with the
      active clock) of them, 't0' and 't1' (their first and last minute),
      'kept' 1 when too few rows kept `previous` (the last good model, whose
      health it keeps), 'prior' 1 when there was no previous model either and
      the values are PRIOR's.

    `minutes` (all, AC too) give your settings and the AC idle clocks; else `rows` do."""
    if len(rows) < MIN_ROWS:
        if previous and 'chip' in previous and not previous.get('prior'):
            return {**PRIOR, **previous, 'kept': 1.0}
        return dict(PRIOR, rows=float(len(rows)), experiment_rows=0.0, active_rows=0.0, kept=0.0, prior=1.0)
    source = rows if minutes is None else minutes
    top = tops(source)
    active = [m['ghz_active'] for m in rows if m.get('ghz_active') is not None]
    spec = {'idle_ghz': float(np.percentile([m['ghz'] for m in rows], IDLE_PERCENTILE)),
            **idle_clocks(rows + [m for m in source if m['st'] != 'D']),
            **{f'top:{key}': float(value) for key, value in top.items() if value is not None}}
    if len(active) >= ACTIVE_MINUTES:
        spec['idle_ghz_active'] = float(np.percentile(active, IDLE_PERCENTILE))
    states = [minute_states(m, top) for m in rows]
    on = {n: sum(s[n] for s in states) for n in SETTING}
    levers = [n for n in SETTING if min(on[n], len(rows) - on[n]) >= STATE_MINUTES]
    yours = own_settings(source, top) if any(not m.get('exp') for m in source if m.get('set')) else None
    spec.update({f'own:{n}': float(yours[n]) if yours else on[n] / len(rows) for n in levers})
    model = dict(fit(*design(rows, spec), levers=levers), **spec)
    model.update(rail_shares(rows, model))
    model.update(rows=float(len(rows)), experiment_rows=float(sum(1 for m in rows if m.get('exp'))),
                 active_rows=float(len(active)), t0=float(rows[0]['t']), t1=float(rows[-1]['t']), kept=0.0, prior=0.0)
    return model


def rail_shares(rows, model):
    """How the idle chip divides between the chip's rails (power.chip_rails):
    {'rail:<rail>': share} (summing to 1) and 'rail_rows', or {} with fewer than
    RAIL_MINUTES rows that measured them. Each rail is fitted on the chip
    model's columns (design), and its idle part is its prediction where the
    chip's baseline is: no load, GPU or video, the idle clock, your settings.
    The rails add up to the chip, so their fits add up to the chip's fit.
    Settings that never changed within these rows are left out (their
    columns would be the intercept's)."""
    rows = [m for m in rows if m.get('vdd') is not None and m.get('fabric') is not None]
    if len(rows) < RAIL_MINUTES:
        return {}
    x = design(rows, model)[0]
    y = np.array([[m['vdd'], m['soc'] - m['vdd'] - m['fabric'], m['fabric']] for m in rows])
    point = np.zeros(x.shape[1])
    point[0], point[CHIP_TERMS.index('clock')] = 1.0, model['idle_ghz']  # Active clock: at its idle, 0.
    varies = np.r_[True, x[:, 1:].std(axis=0) > 1e-9]
    coef = np.linalg.lstsq(x[:, varies], y, rcond=None)[0]
    idle = dict(zip(('cores', 'links', 'fabric'), np.maximum(0.0, point[varies] @ coef)))
    total = sum(idle.values())
    if total <= 0:
        return {}
    return {**{f'rail:{rail}': float(idle[rail] / total) for rail in power.RAILS}, 'rail_rows': float(len(rows))}


def setting_watts(m, model):
    """Battery W the minute's settings added against yours, by the model's
    setting columns (their chip W at k per chip watt, and the rest)."""
    levers = model_levers(model)
    if not levers:
        return 0.0
    states = minute_states(m, model_top(model))
    return sum((model.get(f'bat:{n}', 0.0) + model['chip'] * model.get(f'soc:{n}', 0.0))
               * (states[n] - model[f'own:{n}']) for n in levers)


def typical_power(rows, model):
    """Mean battery W of `rows` (battery_rows) at your settings: each minute's
    draw less what its settings (an experiment's) added against yours."""
    if not rows:
        return None
    return float(np.average([m['bat'] - setting_watts(m, model) for m in rows], weights=[m['dt'] for m in rows]))


def attribute(m, model):
    """Watts for one minute: {'groups': {category: W}, 'apps': {app: W},
    'devices': {device: W}, 'idle': {part: W}} ('idle': the IDLE group's
    parts, power.idle_parts)."""
    soc = m.get('soc')
    groups, apps, devices = collections.defaultdict(float), collections.defaultdict(float), {}
    if soc is None:
        return {'groups': groups, 'apps': apps, 'devices': devices, 'idle': {}}
    on_battery = m['st'] == 'D' and m.get('bat') is not None
    k = model['chip'] if on_battery else 1.0
    if on_battery and soc > 0:
        k = max(0.0, min(k, m['bat'] / soc))  # Less battery draw than k·chip: the stack must not overshoot it.
    idle, raised = power.clock_split(model, m.get('ghz'), profile_of(m), not on_battery, m.get('ghz_active'))
    loads = {name: load for name, (load, _, _) in m.get('apps', {}).items()}
    if m.get('load') is not None:
        loads['Kernel'] = loads.get('Kernel', 0) + max(0.0, m['load'] - sum(loads.values()))
    busy = sum(loads.values())
    if busy <= 0:
        idle += raised  # Nothing ran to raise it.
    floor = min(soc, model['floor'] + idle)
    weights = {}
    for name, load in loads.items():
        _, gpu, video = m.get('apps', {}).get(name, (0, 0, 0))
        weights[name] = (model['load'] * load + model['gpu'] * gpu + model['video'] * video
                         + (raised * load / busy if busy > 0 else 0))
    total = sum(weights.values())
    if total <= 0:
        floor = soc
    for name, weight in weights.items():
        if weight > 0:
            watts = k * (soc - floor) * weight / total
            apps[name] += watts
            groups[group_of(name)] += watts
    groups[IDLE] += k * floor
    idle = power.idle_parts(model, floor, k)
    if on_battery:
        rest = m['bat'] - k * soc
        parts = {name: model[term] * (m.get(term) or 0) for name, term in DEVICES}
        modelled = sum(parts.values())
        shrink = min(1.0, max(0.0, rest) / modelled) if modelled > 0 else 0.0
        devices = {name: watts * shrink for name, watts in parts.items() if watts * shrink > 0.005}
        groups['Display'] += devices.get('Display', 0)
        groups['Rest of system'] += max(0.0, rest - modelled) + sum(w for n, w in devices.items() if n != 'Display')
    return {'groups': groups, 'apps': apps, 'devices': devices, 'idle': idle}


# --- What-if runtime ------------------------------------------------------------

def full_energy(minutes, sys=Path('/sys'), reserve_wh=0.0):
    """Usable Wh of a full charge: charge_full × mean discharge voltage (or
    energy_full), less `reserve_wh`, the energy below the battery's cutoff
    (battery-eta's eta.reserve_wh(): about 0.8 Wh, under 2% of a charge here)."""
    volts = [m['volts'] for m in minutes if m['st'] == 'D' and m.get('volts')]
    for battery in sorted((sys / 'class/power_supply').glob('BAT*')):
        try:
            if (battery / 'energy_full').exists():
                return float((battery / 'energy_full').read_text()) / 1e6 - (reserve_wh or 0.0)
            charge = float((battery / 'charge_full').read_text()) / 1e6
            return charge * (sum(volts) / len(volts) if volts else 15.4) - (reserve_wh or 0.0)
        except (OSError, ValueError):
            continue
    return None


def runtime_gain(energy, power, saved):
    """Minutes of extra runtime from `energy` (usable Wh: full_energy) when
    average draw drops by `saved` W."""
    after = power - saved
    if power <= 0 or after <= 0.5:
        return float('inf') if saved > 0 else 0.0
    return 60 * energy * (1 / after - 1 / power)


def block_indices(n, block, rng):
    """A moving-block bootstrap resample of range(n): ceil(n / block) runs of
    `block` consecutive indices from random starts, cut to n."""
    starts = rng.integers(0, max(1, n - block + 1), size=math.ceil(n / block))
    if n < block:
        return np.arange(n)
    return (starts[:, None] + np.arange(block)).ravel()[:n]


def candidates(rows, model, top=5):
    """Model-based interventions: name → function(coef) → watts saved per row (vector)."""
    usage = collections.Counter()
    members = collections.defaultdict(set)
    for m in rows:
        for name, watts in attribute(m, model)['apps'].items():
            usage[name] += watts * m['dt']
        for name in m.get('apps', {}):
            members[group_of(name)].add(name)

    ghz = np.array([clock_of(m, model) for m in rows])
    active = np.array([np.nan if (a := active_clock(m, model)) is None else a for m in rows], float).reshape(len(rows))
    uses = ~np.isnan(active)
    busy = np.array([max(m.get('load') or 0, sum(v[0] for v in m.get('apps', {}).values())) for m in rows])

    def raised(c):  # W of the clock above idle per row (power.clock_split's), for coefficients c.
        above = c['clock'] * np.maximum(0, ghz - c['idle_ghz'])
        if uses.any():
            above[uses] = c['clock_active'] * np.maximum(0, active[uses] - c['idle_ghz_active'])
        return above

    def activity(names):
        columns = np.zeros((len(rows), 3))
        for i, m in enumerate(rows):
            for name, values in m.get('apps', {}).items():
                if name in names:
                    columns[i] += values
        share = np.divide(columns[:, 0], busy, out=np.zeros(len(rows)), where=busy > 0)  # Of the clock above idle.
        return lambda c: c['chip'] * (c['load'] * columns[:, 0] + raised(c) * share
                                      + c['gpu'] * columns[:, 1] + c['video'] * columns[:, 2])
    items = {}
    phrases = {'Browser': 'Close the browser', 'Builds & EDA': 'No builds or EDA jobs',
               'Coding tools': 'Close coding tools', 'Chat & media': 'Close chat & media',
               'Other apps': 'Close other apps'}
    for group in APP_GROUPS:
        if group in phrases and members.get(group):
            items[phrases[group]] = activity(members[group])
    apps = [name for name, _ in usage.most_common() if name != 'Kernel' and group_of(name) != 'Desktop & system']
    for name in apps[:top]:
        items[f'Close {display_name(name)}'] = activity({name})
    bl = np.array([m.get('bl') or 0 for m in rows])
    items['Brightness −20 points'] = lambda c: c['bl'] * np.clip(bl - 0.05, 0, 0.2)
    items['Brightness at minimum'] = lambda c: c['bl'] * np.clip(bl - 0.05, 0, None)
    kbd = np.array([m.get('kbd') or 0 for m in rows])
    if kbd.any():
        items['Keyboard backlight off'] = lambda c: c['kbd'] * kbd
    return items


def resample_groups(rows, block, days=None):
    """The groups of consecutive rows what_if resamples, as their boundaries,
    and whether they are days: whole days once the rows span `days` of them,
    else runs of `block` rows."""
    dates = [datetime.date.fromtimestamp(m['t']) for m in rows]
    if days is not None and len(set(dates)) >= days:
        return [0, *(i for i in range(1, len(rows)) if dates[i] != dates[i - 1]), len(rows)], True
    return [*range(0, len(rows), block), len(rows)], False


DAY_GROUPS = 7  # Days of rows before what_if resamples whole days.


def what_if(rows, energy, replicates=200, block=60, seed=0, model=None, train_rows=None, days=DAY_GROUPS):
    """Point estimate and 90% interval of runtime gain (minutes) per intervention.

    `model` is the shared trained model (battery-eta's models.json), evaluated
    on this period's `rows`; without one, the models are fitted on
    `train_rows`. The period's draw is at your settings (typical_power). The
    interval refits both models on bootstrap resamples of the training rows
    and averages over resamples of the period, so it covers coefficient
    uncertainty as well as the period's variability. Both resample whole days
    once they span `days` of them (resample_groups), else `block`-minute runs:
    residuals stay correlated for an hour and more (0.34 at 10 minutes, 0.2 at
    60), and brightness drifts slower still. Simulated so (12 days of 2 h),
    90% intervals for dimming covered 48% with the 10-minute blocks used
    before, 78% with hour blocks and 87% with whole days (and Student's t over
    them: few days spread too little); for closing an app 87%, 100% and 100%.
    Training groups don't overlap, so a resample just adds up each one's sums.
    """
    train_rows = train_rows or rows
    if len(rows) < 30 or len(train_rows) < 30 or not energy:
        return [], None
    model = model or fit_rows(train_rows)
    train = design(train_rows, model)
    levers = model_levers(model)
    # Each minute's settings against yours (design's columns), at their W each.
    effect = np.array([model.get(f'bat:{n}', 0.0) + model['chip'] * model.get(f'soc:{n}', 0.0) for n in levers])
    period = design(rows, model)
    adjust = period[2][:, len(BATTERY_TERMS):] @ effect if levers else np.zeros(len(rows))
    battery_y = period[3] - adjust
    items = candidates(rows, model)
    dt = np.array([m['dt'] for m in rows])

    def evaluate(index, coef):
        power = float(np.average(battery_y[index], weights=dt[index]))
        return power, {name: runtime_gain(energy, power, float(np.average(vector(coef)[index], weights=dt[index])))
                       for name, vector in items.items()}
    power, point = evaluate(np.arange(len(rows)), model)
    saved = {name: float(np.average(vector(model), weights=dt)) for name, vector in items.items()}
    rng = np.random.default_rng(seed)
    samples = collections.defaultdict(list)
    idle = {key: value for key, value in model.items() if key.startswith('idle_ghz')}  # Not refitted: clocks only.
    bounds, _ = resample_groups(train_rows, block, days)
    sums = [np.array(part) for part in zip(*(gram(train[0][a:b], train[1][a:b]) + gram(train[2][a:b], train[3][a:b])
                                             for a, b in zip(bounds, bounds[1:])))]
    parts, by_day = resample_groups(rows, block, days)
    parts = [np.arange(a, b) for a, b in zip(parts, parts[1:])] if by_day else None  # Else moving blocks.
    for _ in range(replicates):
        counts = np.bincount(rng.integers(0, len(bounds) - 1, len(bounds) - 1), minlength=len(bounds) - 1).astype(float)
        total = [np.tensordot(counts, part, 1) for part in sums]
        coef = dict(fit(*train, levers=levers, sums=(total[:2], total[2:])), **idle)
        index = (np.concatenate([parts[i] for i in rng.integers(0, len(parts), len(parts))]) if parts
                 else block_indices(len(rows), block, rng))
        for name, gain in evaluate(index, coef)[1].items():
            samples[name].append(gain)
    # Few groups (days) spread too little: widen as Student's t over them would.
    groups = len(bounds) - 1
    widen = float(stdtrit(max(groups - 1, 1), 0.95) / 1.6449 * math.sqrt(groups / max(groups - 1, 1)))
    estimates = []
    for name, gain in point.items():
        low, high = np.percentile(samples[name], [5, 95])
        estimates.append({'name': name, 'evidence': 'model', 'watts': saved[name], 'minutes': gain,
                          'low': float(min(gain - widen * (gain - low), gain)),
                          'high': float(max(gain + widen * (high - gain), gain))})
    return (sorted(estimates, key=lambda e: -e['minutes']),
            {'power': power, 'energy': energy, 'model': model, 'runtime_h': energy / power, 'rows': len(rows),
             'experiment_rows': sum(1 for m in rows if m.get('exp')), 'train_rows': len(train_rows),
             'settings_w': float(np.average(adjust, weights=dt))})


# --- Experiments ------------------------------------------------------------------

def clean_minutes(minutes):
    """Experiment minutes outside washout: a minute's clean parts
    (add_clean_parts) where it has them, else the minute unless it is washout."""
    for m in minutes:
        if m.get('exp'):
            if 'clean' in m:
                yield from m['clean']
            elif not m['exp'][4]:
                yield m


def experiment_blocks(minutes):
    blocks = collections.defaultdict(lambda: [0.0, 0.0, None, None, None])
    for m in clean_minutes(minutes):
        exp = m['exp']
        if exp[4] or m['st'] != 'D' or m.get('bat') is None:
            continue
        run, name, block, arm, _, value = exp
        entry = blocks[(run, block)]
        entry[0] += m['bat'] * m['dt']
        entry[1] += m['dt']
        entry[2:] = [name, arm, value]
    return {key: {'name': v[2], 'arm': v[3], 'value': v[4], 'watts': v[0] / v[1], 'seconds': v[1]}
            for key, v in blocks.items() if v[1] >= 90}


def by_value(blocks, arm):
    values = [b['watts'] for b in blocks if b['arm'] == arm]
    return sum(values) / len(values) if values else None


T_PAIRS = 30  # Fewer pairs than this: a t interval (a bootstrap of 3 pairs covered ~70% for 90%).


def experiment_effects(minutes, energy, power, replicates=2000, seed=0):
    """Paired consecutive A/B blocks per run → saving of arm B (W) and runtime gain.
    Only runs of one setting (app freezing): a combined run's arm is a string
    of letters and its value a dict per setting, which lever_effects fits.
    The 90% interval is Student's t below T_PAIRS pairs, else a bootstrap of pairs."""
    blocks = {key: b for key, b in experiment_blocks(minutes).items()
              if b['arm'] in ('A', 'B') and not isinstance(b['value'], (dict, list)) and key[1] is not None}
    pairs = collections.defaultdict(list)
    arms = collections.defaultdict(list)
    for block in blocks.values():
        arms[block['name']].append(block)
    for run in sorted({key[0] for key in blocks}):
        sequence = sorted(((key[1], b) for key, b in blocks.items() if key[0] == run), key=lambda item: item[0])
        i = 0
        while i + 1 < len(sequence):
            (n1, first), (n2, second) = sequence[i], sequence[i + 1]
            if first['arm'] != second['arm'] and n2 == n1 + 1:
                a, b = (first, second) if first['arm'] == 'A' else (second, first)
                pairs[(a['name'], b['value'])].append(a['watts'] - b['watts'])
                i += 2
            else:
                i += 1
    rng = np.random.default_rng(seed)
    results = []
    for (name, value), deltas in pairs.items():
        deltas = np.array(deltas)
        estimate = {'experiment': name, 'value': value, 'pairs': len(deltas), 'evidence': 'experiment',
                    'watts': float(deltas.mean()), 'a_watts': by_value(arms[name], 'A'),
                    'b_watts': by_value(arms[name], 'B')}
        if len(deltas) >= 3 and energy and power:
            if len(deltas) < T_PAIRS:
                half = stdtrit(len(deltas) - 1, 0.95) * deltas.std(ddof=1) / math.sqrt(len(deltas))
                low, high = deltas.mean() - half, deltas.mean() + half
            else:
                low, high = np.percentile(rng.choice(deltas, size=(replicates, len(deltas))).mean(axis=1), [5, 95])
            estimate.update(minutes=runtime_gain(energy, power, estimate['watts']),
                            low=runtime_gain(energy, power, float(low)),
                            high=runtime_gain(energy, power, float(high)),
                            watts_low=float(low), watts_high=float(high))
        results.append(estimate)
    return results


BOOT_MINUTES = 10  # Battery minutes for a boot to count.
MIN_BOOTS = 5  # Boots per arm before boot_effect estimates anything.


def boot_effect(minutes, energy, power, key='psr', min_boots=MIN_BOOTS, min_rows=30):
    """Boot-level setting effect on battery draw, adjusted for workload and settings.

    Least squares of battery W on load, GPU/video, backlight, Wi-Fi and disk,
    the settings that vary within boots (the profile, an experiment's) and a
    baseline per boot; the setting's saving is the difference of the boot
    baselines between arms, with a Welch t interval over boots (minutes inside
    a boot are not independent, so boots are what is counted). Returns progress
    counts until each arm has `min_boots` boots with BOOT_MINUTES on battery.
    Simulated with boots' own baselines 1 W apart, the old bootstrap of 2
    boots against 7 covered 69% for a nominal 90%; this covers 90% from 5
    boots each."""
    rows = [m for m in battery_rows(minutes) if m.get('boot')]
    arms = {False: collections.defaultdict(list), True: collections.defaultdict(list)}
    for m in rows:
        arms[bool(m['set'].get(key))][m['boot']].append(m)
    progress = {'experiment': key, 'evidence': 'boot', 'value': 'on',
                'boots': {'off': len(arms[False]), 'on': len(arms[True])},
                'hours': {'off': sum(len(v) for v in arms[False].values()) / 60,
                          'on': sum(len(v) for v in arms[True].values()) / 60}}
    counted = {arm: {b: v for b, v in boots.items() if len(v) >= BOOT_MINUTES} for arm, boots in arms.items()}
    if min(len(counted[False]), len(counted[True])) < max(2, min_boots) or len(rows) < 2 * min_rows:
        return progress
    boots = [(arm, b) for arm in (False, True) for b in counted[arm]]
    used = [m for arm, b in boots for m in counted[arm][b]]
    top = tops(minutes)
    states = [minute_states(m, top) for m in used]
    index = {b: i for i, (_, b) in enumerate(boots)}

    def varies_within(name):
        on = collections.defaultdict(collections.Counter)
        for m, s in zip(used, states):
            on[m['boot']][s[name]] += 1
        return any(min(c[True], c[False]) >= BOOT_MINUTES for c in on.values())
    levers = [n for n in SETTING if varies_within(n)]
    x = np.zeros((len(used), len(boots) + 6 + len(levers)))
    for i, (m, s) in enumerate(zip(used, states)):
        x[i, index[m['boot']]] = 1.0
        x[i, len(boots):] = [m['load'], m.get('gpu', 0), m.get('video', 0), m.get('bl') or 0, m.get('wifi', 0),
                             m.get('disk', 0), *(float(s[n]) for n in levers)]
    base = np.linalg.lstsq(x, np.array([m['bat'] for m in used]), rcond=None)[0][:len(boots)]
    on, off = (np.array([base[i] for i, (arm, _) in enumerate(boots) if arm == side]) for side in (True, False))
    saved = float(off.mean() - on.mean())
    v_on, v_off = on.var(ddof=1) / len(on), off.var(ddof=1) / len(off)
    se = math.sqrt(v_on + v_off)
    df = (v_on + v_off) ** 2 / (v_on ** 2 / (len(on) - 1) + v_off ** 2 / (len(off) - 1)) if se > 0 else 1.0
    half = float(stdtrit(df, 0.95)) * se
    low, high = saved - half, saved + half
    progress.update(watts=saved, watts_low=low, watts_high=high, df=float(df), adjusted=levers)
    if energy and power:
        progress.update(minutes=runtime_gain(energy, power, saved), low=runtime_gain(energy, power, low),
                        high=runtime_gain(energy, power, high))
    return progress


# --- Levers: settings measured together ----------------------------------------------

# Settings measured by experiment (names match experiment.catalog()) → their
# key in a minute's `set`.
SETTING = {'aspm': 'aspm', 'apst': 'apst', 'wifi-ps': 'wifi_ps', 'abm': 'abm', 'refresh': 'hz',
           'boost': 'boost', 'profile': 'profile', 'gpu-dpm': 'gpu_dpm', 'cpu-cap': 'cpu_cap',
           'throttle-apps': 'throttle'}


def saving(name, value, top_hz=None):
    """Is `value` — as a minute logs it, or as power-experiment sets it — the
    setting's power-saving state?"""
    if value is None:
        return False
    try:
        if name == 'aspm':
            return value in ('powersave', 'powersupersave')
        if name == 'apst':
            return str(value) != '0'
        if name == 'wifi-ps':
            return set(str(value).split(',')) == {'on'}
        if name == 'abm':
            return int(value) > 0
        if name == 'refresh':  # 60.0 per minute, '2256x1504@47.998' from the runner
            return top_hz is not None and float(str(value).rpartition('@')[2]) < top_hz - 1
        if name == 'boost':
            return str(value) == '0'
        if name == 'profile':  # platform_profile per minute, power-profiles-daemon's name from the runner
            return value in ('low-power', 'quiet', 'power-saver')
        if name in ('gpu-dpm', 'throttle-apps'):
            return value in ('low', 'on')
        if name == 'cpu-cap':  # The runner's value; minutes compare max_mhz with the CPU's maximum.
            return str(value) != 'none'
    except (TypeError, ValueError):
        return False
    return False


# These change CPU frequency, and so the CPU load the workload correction
# uses. A consequence of the setting must not be adjusted away, so they are
# estimated without that correction and need more battery time.
# gpu-dpm: memory clocks slow CPU work too; throttle-apps: it cuts the load itself.
CPU_LEVERS = {'boost', 'profile', 'cpu-cap', 'gpu-dpm', 'throttle-apps'}
COVARIATES = ('load', 'gpu', 'video', 'bl', 'kbd', 'wifi', 'disk', 'usb', 'audio')
PRIOR_SD = {'main': 5.0, 'pair': 1.0}  # W: interactions are shrunk (mildly) toward zero.
BLOCK_MINUTES = 4  # power-experiment's blocks (experiment.BLOCK); the first minute is washout.


def lever_states(settings, top, chosen=None):
    """{name: in its saving state}; `top` is tops(minutes). `chosen` — the values an
    experiment set for the settings it randomized — wins over what was logged:
    logging can lag, and before 2026-10-07 the APST value logged was nvme_core's
    boot default. Boost is the exception. Until 2026-10-08 experiments set, and
    minutes logged, only the global switch, while power-profiles-daemon turns
    boost off per CPU for power-saver; most "on" blocks under power-saver ran
    without it. So it comes from the CPU's frequency cap: off while capped below
    the highest cap seen."""
    try:  # A month of minutes has few distinct settings: each is worked out once.
        memo = (tuple(settings.items()), tuple(top.items()), tuple((chosen or {}).items()))
        return dict(STATES[memo])
    except TypeError:  # Something unhashable: work it out.
        memo = None
    except KeyError:
        pass
    chosen = chosen or {}
    states = {name: saving(name, chosen[name] if name in chosen else settings.get(key), top.get('hz'))
              for name, key in SETTING.items()}
    # The CPU's own maximum (boost) is cpu_mhz since layout 6; before, no cap was
    # ever set, so the cap (mhz) was it.
    highest = max(top.get('mhz') or 0, top.get('cpu_mhz') or 0)
    maximum = settings.get('cpu_mhz') or settings.get('mhz')
    if maximum is not None and highest:
        states['boost'] = maximum < highest - 100
    if settings.get('cpu_mhz') and settings.get('mhz') is not None:
        states['cpu-cap'] = settings['mhz'] < settings['cpu_mhz'] - 100
    if memo is not None:
        if len(STATES) > 10000:
            STATES.clear()
        STATES[memo] = dict(states)
    return states


STATES = {}  # lever_states' answers.


def own_settings(minutes, top=None):
    """Your settings, as saving states: the latest minute's outside experiments
    (the runner puts them back), except AC_MANAGED ones. Those are what most of
    the last OWN_MINUTES battery minutes outside experiments had (a single one
    can be from just before udev switched, or from trying balanced for a while),
    or udev's battery states (UDEV_BATTERY) when there are none: AC minutes
    have AC's."""
    top = top or tops(minutes)
    logged = [m for m in minutes if m.get('set')]
    own = [m for m in logged if not m.get('exp')] or logged
    states = lever_states(own[-1]['set'] if own else {}, top)
    battery = [m for m in own if m['st'] == 'D' and not m.get('exp')][-OWN_MINUTES:]
    if battery:
        votes = collections.Counter((n, on) for m in battery for n, on in lever_states(m['set'], top).items()
                                    if n in AC_MANAGED)
        states.update({n: votes[n, True] > votes[n, False] for n in AC_MANAGED})
    else:
        states.update(UDEV_BATTERY)
    return states


# udev switches the profile and ABM on AC/battery changes, and each profile
# change sets boost (power-profiles-daemon: off for power-saver).
AC_MANAGED = {'profile', 'abm', 'boost'}
UDEV_BATTERY = {'profile': True, 'abm': True, 'boost': True}  # Their states on battery: power-saver, ABM on, no boost.
OWN_MINUTES = 600


def experiment_values(exp, levers):
    """The values a minute's experiment set: a dict for several settings, a value for one
    (none if it logged none: then the minute's own settings say)."""
    return exp[5] if isinstance(exp[5], dict) else {levers[0]: exp[5]} if len(levers) == 1 and exp[5] is not None else {}


def tops(minutes):
    """The highest refresh rate and CPU frequency cap logged: their normal states."""
    return {key: max((m['set'][key] for m in minutes if (m.get('set') or {}).get(key)), default=None)
            for key in ('hz', 'mhz', 'cpu_mhz')}


def block_group(run, t):
    """A block's baseline group: its run and day (an automatic run goes on for weeks)."""
    return f"{run}@{datetime.date.fromtimestamp(t).isoformat()}"


def lever_blocks(minutes):
    """Clean blocks of setting experiments (single or combined): battery W and
    workload means, and which settings were in their saving state. From clean
    parts (records outside washout) where minutes have them."""
    top = tops(minutes)
    acc = {}
    for m in clean_minutes(minutes):
        exp = m['exp']
        if (exp[4] or m['st'] != 'D' or m.get('bat') is None or m.get('load') is None
                or m['dt'] < (0 if m.get('part') else 40)):
            continue
        levers = [name for name in str(exp[1]).split('+') if name in SETTING]
        if not levers:
            continue  # App-freezing runs are analysed as paired A/B blocks.
        chosen = experiment_values(exp, levers)
        group = block_group(exp[0], m['t'])
        block = acc.setdefault((exp[0], exp[2]), {'run': group, 'levers': levers, 't': m['t'], 'seconds': 0.0,
                                                  'sums': collections.defaultdict(float),
                                                  'states': collections.Counter()})
        block['seconds'] += m['dt']
        for key in ('bat', *COVARIATES):
            block['sums'][key] += (m.get(key) or 0) * m['dt']
        block['states'][tuple(lever_states(m['set'], top, chosen).items())] += m['dt']
    blocks = []
    for block in acc.values():
        if block['seconds'] < 90:
            continue
        row = {key: value / block['seconds'] for key, value in block['sums'].items()}
        row.update(run=block['run'], levers=block['levers'], t=block['t'], seconds=block['seconds'],
                   x=dict(block['states'].most_common(1)[0][0]))
        blocks.append(row)
    return sorted(blocks, key=lambda b: b['t'])


def ridge(x, y, w, penalty):
    """Weighted ridge regression, argmin Σ w (y − xβ)² + Σ penalty β², from its
    normal equations (xᵀWx + diag(penalty)) β = xᵀWy: about 25× faster than a
    least squares on the stacked rows for these small systems."""
    xw = x * w[:, None]
    a, b = xw.T @ x + np.diag(penalty), xw.T @ y
    try:
        return np.linalg.solve(a, b)  # Every penalty is positive, so a is positive definite.
    except np.linalg.LinAlgError:
        return np.linalg.lstsq(a, b, rcond=None)[0]


# A setting's consequences must not be adjusted away, so each is estimated
# without the covariates it moves: the CPU settings the CPU load, and ASPM and
# APST the disk's busy time (its link wakes slower). A pair leaves out both
# settings' mediators.
MEDIATORS = {**{name: ('load',) for name in CPU_LEVERS}, 'aspm': ('disk',), 'apst': ('disk',)}
# Pairs expected to interact before any data (both set the CPU's clock): judged
# at 90% on their own, and the only pairs best_config chooses with.
PRESPECIFIED = (frozenset({'profile', 'cpu-cap'}), frozenset({'boost', 'cpu-cap'}))
PAIR_FDR = 0.1  # Other pairs are 'clear' at this false discovery rate (Benjamini-Hochberg).


def mediators(key):
    """The covariates a setting, or a pair of them, is estimated without."""
    return frozenset(c for name in (key if isinstance(key, tuple) else (key,)) for c in MEDIATORS.get(name, ()))


def lever_rows(blocks, runs, covariates, mains, pairs=()):
    """Design rows: a baseline per run, workload covariates, settings' and pairs' saving states."""
    return np.array([[*(float(b['run'] == run) for run in runs), *(b[c] for c in covariates),
                      *(float(b['x'][n]) for n in mains), *(float(b['x'][a] and b['x'][c]) for a, c in pairs)]
                     for b in blocks]).reshape(len(blocks), len(runs) + len(covariates) + len(mains) + len(pairs))


def block_weights(blocks):
    """Each block's weight: its clean seconds against the mean (σ is per average block)."""
    w = np.array([b['seconds'] for b in blocks], float)
    return w / w.mean() if len(w) else w


def block_sigma(blocks, covariates=COVARIATES, names=None, spare=10):
    """Block-to-block noise (W) after each run's baseline, the workload and the
    settings (`names` that vary; all of them by default), as lever_effects
    estimates it; None without `spare` blocks beyond the coefficients. Without
    the run baselines it read 2.45 W instead of 1.69."""
    runs = sorted({b['run'] for b in blocks})
    names = [n for n in (SETTING if names is None else names) if len({b['x'].get(n) for b in blocks}) > 1]
    k = len(runs) + len(covariates) + len(names)
    if len(blocks) < k + spare:
        return None
    x, y, w = lever_rows(blocks, runs, covariates, names), np.array([b['bat'] for b in blocks]), block_weights(blocks)
    resid = y - x @ ridge(x, y, w, np.full(k, 1e-6))
    return float(np.sqrt(np.sum(w * resid ** 2) / (len(y) - k)))


def benjamini_hochberg(p, q):
    """Which of the p-values `p` are discoveries at false discovery rate `q`."""
    order = np.argsort(p)
    passed = [i for i, j in enumerate(order) if p[j] <= q * (i + 1) / len(p)]
    found = np.zeros(len(p), bool)
    found[order[:passed[-1] + 1] if passed else []] = True
    return found


def lever_effects(minutes, energy, power, replicates=300, block=3, seed=0, min_blocks=3):
    """Savings of each experimented setting, and of pairs of them together.

    One regression over clean blocks of every setting experiment: battery W on
    a baseline per run (so a setting is only ever compared within the run that
    randomized it), workload, an indicator per setting (saving state) and per
    pair of settings randomized together (shrunk toward zero: most pairs don't
    interact). A setting comes from a fit without its MEDIATORS (CPU settings
    without the CPU load, ASPM and APST without the disk). Effects are for your
    settings (own_settings), with `chance`, the probability each saves.
    Intervals: moving-block bootstrap over consecutive blocks. Interactions
    carry `p` (two-sided), `prespecified`, and `clear`: a PRESPECIFIED pair at
    p < 0.1, any other at a PAIR_FDR false discovery rate over all of them (45
    pairs at 90% would show ~4.5 by chance).
    """
    blocks = lever_blocks(minutes)
    runs = sorted({b['run'] for b in blocks})
    randomized = {name for b in blocks for name in b['levers']}

    def split(subset, name):
        return {'on': sum(1 for b in subset if b['x'][name]), 'off': sum(1 for b in subset if not b['x'][name])}

    def within(name):  # Varied inside some run, so a run baseline cannot absorb it.
        return any(min(split([b for b in blocks if b['run'] == run], name).values()) >= min_blocks for run in runs)
    varying = [n for n in SETTING if within(n)]
    shown = [n for n in varying if n in randomized]
    together = lambda a, c: [b for b in blocks if a in b['levers'] and c in b['levers']]
    pairs = [(a, c) for i, a in enumerate(shown) for c in shown[i + 1:]
             if all(sum(1 for b in together(a, c) if (b['x'][a], b['x'][c]) == combo) >= min_blocks
                    for combo in ((False, False), (False, True), (True, False), (True, True)))]
    current = own_settings(minutes)
    result = {'effects': [], 'interactions': [], 'blocks': len(blocks), 'current': current, 'coef': {},
              'samples': {}, 'sigma': None, 'sigma_raw': None,
              'progress': {n: split([b for b in blocks if n in b['levers']], n) for n in randomized}}
    if not shown or len(blocks) < len(runs) + len(COVARIATES) + len(varying) + len(pairs) + 5:
        return result
    keys = (*varying, *pairs)
    fits = sorted({mediators(key) for key in keys}, key=sorted)
    covariates = {k: tuple(c for c in COVARIATES if c not in k) for k in {*fits, frozenset(), frozenset({'load'})}}
    y, w = np.array([b['bat'] for b in blocks]), block_weights(blocks)
    # One design with every covariate; each fit is its columns without its mediators.
    x = lever_rows(blocks, runs, COVARIATES, varying, pairs)
    columns = {k: np.r_[np.arange(len(runs)), [len(runs) + i for i, c in enumerate(COVARIATES) if c not in k],
                        np.arange(len(runs) + len(COVARIATES), x.shape[1])].astype(int) for k in covariates}
    owned = {k: [(i, key) for i, key in enumerate(keys) if mediators(key) == k] for k in fits}

    def noise(k):  # Residual variance of the main-effects fit, per block (block_sigma).
        main = columns[k][:len(runs) + len(covariates[k]) + len(varying)]
        resid = y - x[:, main] @ ridge(x[:, main], y, w, np.full(len(main), 1e-6))
        return float(np.sum(w * resid ** 2) / max(1, len(y) - len(main)))
    sigma2 = {k: noise(k) for k in covariates}
    penalty = {k: np.array([1e-6] * (len(runs) + len(covariates[k]))
                           + [max(sigma2[k], 1e-6) / PRIOR_SD['main'] ** 2] * len(varying)
                           + [max(sigma2[k], 1e-6) / PRIOR_SD['pair'] ** 2] * len(pairs)) for k in fits}
    # best_config's model ('select'): main effects and PRESPECIFIED pairs only, so
    # a setting's effect is its average over the others as they were randomized,
    # not one moved by each noisy estimate of its pairs with them.
    chosen = np.array([i for i, pair in enumerate(pairs) if frozenset(pair) in PRESPECIFIED], int)
    lean = {k: np.r_[columns[k][:len(runs) + len(covariates[k]) + len(varying)],
                     columns[k][len(runs) + len(covariates[k]) + len(varying) + chosen]].astype(int) for k in fits}
    lean_keys = (*varying, *(pairs[i] for i in chosen))
    lean_owned = {k: [(i, key) for i, key in enumerate(lean_keys) if mediators(key) == k] for k in fits}

    def fit(index):
        # A resample as weights (how often each block was drawn): the same sums as
        # its rows, without copying them; and one product for every fit.
        g, gy = gram(x, y, w * np.bincount(index, minlength=len(blocks)))
        coef, select = {}, {}
        for k in fits:
            base = len(runs) + len(covariates[k])
            for keep, own, out in ((columns[k], owned[k], coef), (lean[k], lean_owned[k], select)):
                a = g[np.ix_(keep, keep)] + np.diag(penalty[k][np.searchsorted(columns[k], keep)])
                try:
                    beta = np.linalg.solve(a, gy[keep])
                except np.linalg.LinAlgError:
                    beta = np.linalg.lstsq(a, gy[keep], rcond=None)[0]
                out.update((key, beta[base + i]) for i, key in own)
        return coef, select
    coef, select = fit(np.arange(len(blocks)))
    rng = np.random.default_rng(seed)
    draws = [fit(block_indices(len(blocks), block, rng)) for _ in range(replicates)]
    samples = {key: np.array([d[0][key] for d in draws]) for key in coef}
    result['select'] = {'coef': select, 'samples': {key: np.array([d[1][key] for d in draws]) for key in select}}
    hours = lambda name: sum(b['seconds'] for b in blocks if name in b['levers']) / 3600

    def summary(saving, values):
        low, high = np.percentile(values, [5, 95])
        sd = float(np.std(values))
        estimate = {'watts': float(saving), 'watts_low': float(low), 'watts_high': float(high),
                    'chance': float(ndtr(saving / sd)) if sd > 0 else float(saving > 0)}
        if energy and power:
            estimate.update(minutes=runtime_gain(energy, power, estimate['watts']),
                            low=runtime_gain(energy, power, estimate['watts_low']),
                            high=runtime_gain(energy, power, estimate['watts_high']))
        return estimate

    def lever(c, name):
        """W saved by a setting's saving state, the others as they are now."""
        return predicted(c, dict(current, **{name: False})) - predicted(c, dict(current, **{name: True}))
    # predicted() takes the samples whole: each coefficient is an array of draws.
    result['effects'] = [dict(summary(lever(coef, n), lever(samples, n)),
                              experiment=n, evidence='experiment', cpu=n in CPU_LEVERS,
                              blocks=result['progress'][n], hours=hours(n), current=current[n]) for n in shown]
    # Extra W saved with both of a pair on, beyond the two separately.
    interactions = [dict(summary(-coef[pair], -samples[pair]), pair=pair,
                         prespecified=frozenset(pair) in PRESPECIFIED) for pair in pairs]
    for i in interactions:
        sd = float(np.std(samples[i['pair']]))
        i['p'] = float(2 * ndtr(-abs(i['watts']) / sd)) if sd > 0 else float(i['watts'] == 0)
    others = [i for i in interactions if not i['prespecified']]
    for i, found in zip(others, benjamini_hochberg(np.array([i['p'] for i in others]), PAIR_FDR)):
        i['clear'] = bool(found)
    for i in interactions:
        if i['prespecified']:
            i['clear'] = i['p'] < 0.1
    result['interactions'] = interactions
    result.update(coef=coef, samples=samples, sigma=math.sqrt(sigma2[frozenset()]),
                  sigma_raw=math.sqrt(sigma2[frozenset({'load'})]))
    return result


def predicted(coef, config):
    """Battery W relative to every measured setting off, for `config` {name: on}."""
    return sum(value * (all(config.get(n) for n in key) if isinstance(key, tuple) else config.get(key, False))
               for key, value in coef.items())


CONFIDENCE = 0.9  # A change is recommended only if it saves with at least this chance.
# W: the least spread of settings' effects the shrinkage assumes. Most effects
# are near zero and a few large, so their spread estimated alone can come out
# ~0 and shrink a real 2 W saving away with them.
EFFECT_SD = 0.3


def selection(result):
    """What best_config chooses with, as (terms, shrink factor per term, fitted
    values, bootstrap draws): lever_effects' 'select' fit, each setting's main
    effect and the PRESPECIFIED pairs (without one, its coefficients of those).
    Other pairs don't take part: changing two settings saves what each does on
    average (and a PRESPECIFIED pair). The settings' effects are shrunk toward
    zero by an empirical-Bayes factor τ²/(τ² + se²), τ² the spread of all of
    them beyond their noise: the largest of many noisy estimates is the
    likeliest to be too large."""
    lean = result.get('select') or {'coef': result['coef'], 'samples': result['samples']}
    terms = [k for k in lean['coef'] if not isinstance(k, tuple) or frozenset(k) in PRESPECIFIED]
    point = np.array([lean['coef'][k] for k in terms], float)
    draws = np.array([lean['samples'][k] for k in terms], float).reshape(len(terms), -1)
    mains = np.array([not isinstance(k, tuple) for k in terms])
    var = draws.var(axis=1)
    tau2 = max(EFFECT_SD ** 2, float(np.mean(point[mains] ** 2 - var[mains]))) if mains.any() else 0.0
    factor = np.where(mains, tau2 / np.maximum(tau2 + var, 1e-12), 1.0)
    return terms, factor, point, draws


def best_config(result, allowed, energy=None, power=None, feasible=None, key=None, exclude=()):
    """The measured combination of `allowed` settings (less `exclude`; others
    stay as they are) and its battery saving against yours.

    Picking the lowest of 2^10 point predictions found savings where there were
    none. So: main effects and PRESPECIFIED pairs only, shrunk (selection),
    and changes one at a time — the largest saving first — only while a
    change saves with at least CONFIDENCE chance (at that false discovery
    rate over all of `allowed`) and `feasible` accepts the result. With a
    `key` (lower is better; ties broken by fewest differences from that and
    then by the shrunk draw) any feasible combination may win instead. The
    interval re-selects in each bootstrap draw, so it carries the selection's
    own uncertainty; `chance` is each change's probability of saving, the
    others as chosen, and `stable` the share of draws that chose the same
    combination. Simulated over 10 settings without any effect, the old way
    reported +1.9 W and its interval left out 0 three times in four; this
    +0.04 W, never. With three that save (3.4 W together) it changed 0.3
    settings that don't (old: 3.9), reported 3.34 W for a true 3.44 (old: 4.86
    for 3.36), and its interval covered that 93% of the time (old: 45%)."""
    names = [e['experiment'] for e in result['effects']
             if e['experiment'] in allowed and e['experiment'] not in exclude]
    if not names:
        return None
    current = {n: bool(result['current'].get(n, False)) for n in names}
    terms, factor, point, draws = selection(result)

    def phi(config):  # A configuration's indicators over the terms.
        state = dict(result['current'], **config)
        return np.array([float(all(state.get(n) for n in t)) if isinstance(t, tuple) else float(bool(state.get(t)))
                         for t in terms])
    shrunk = factor * point
    posterior = shrunk[:, None] + np.sqrt(factor)[:, None] * (draws - point[:, None])
    cov = np.atleast_2d(np.cov(posterior)) if posterior.shape[1] > 1 else np.zeros((len(terms), len(terms)))
    ok = (lambda c: True) if feasible is None else (lambda c: feasible(dict(result['current'], **c)))

    def chance(saved, v):
        sd = math.sqrt(max(float(v @ cov @ v), 0.0))
        return float(ndtr(saved / sd)) if sd > 0 else float(saved > 0)

    def eligible(theta):
        # Of ten settings, one in ten would pass CONFIDENCE without saving
        # anything: only those that pass at that false discovery rate over all
        # of them (Benjamini-Hochberg) may change.
        start = phi(current)
        vs = [start - phi(dict(current, **{n: not current[n]})) for n in names]
        found = benjamini_hochberg(np.array([1 - chance(float(v @ theta), v) for v in vs]), 1 - CONFIDENCE)
        return [n for n, f in zip(names, found) if f]

    def greedy(theta):
        config, candidates = dict(current), eligible(theta)
        while True:
            here, best = phi(config), None
            for n in candidates:
                flipped = dict(config, **{n: not config[n]})
                v = here - phi(flipped)
                saved = float(v @ theta)
                if saved > 0 and chance(saved, v) >= CONFIDENCE and ok(flipped) and (best is None or saved > best[0]):
                    best = (saved, flipped)
            if best is None:
                return config
            config = best[1]
    configs, ranks = None, None
    if key is not None:  # Every feasible combination, ranked by the key once (it doesn't change between draws).
        configs = [c for c in (dict(zip(names, combo)) for combo in itertools.product((False, True), repeat=len(names)))
                   if ok(c) or c == current]
        keys = [key(dict(result['current'], **c)) for c in configs]
        rank = {k: i for i, k in enumerate(sorted(set(keys)))}
        ranks = np.array([rank[k] for k in keys])
        table = np.array([phi(c) for c in configs])
        states = np.array([[c[n] for n in names] for c in configs])

    def choose(theta):
        chosen = greedy(theta)
        if key is None:
            return chosen
        best = ranks == ranks.min()
        apart = (states != np.array([chosen[n] for n in names])).sum(axis=1)
        order = np.lexsort((table @ theta, apart, ~best))
        return configs[order[0]]
    best = choose(shrunk)
    saving = float((phi(current) - phi(best)) @ shrunk)
    # Each draw chooses as the data would have (its shrunk estimates) and is
    # scored by its posterior draw: shrunk draws alone vary only factor² as
    # much as the posterior, and covered the true saving 83–88% of the time
    # for 90% (simulated: 3 real effects among 10 settings); this 93%.
    picks = [choose(factor * draws[:, b]) for b in range(draws.shape[1])]
    spread = [float((phi(current) - phi(c)) @ posterior[:, b]) for b, c in enumerate(picks)]
    low, high = np.percentile(spread, [5, 95]) if spread else (saving, saving)
    changes = [n for n in names if best[n] != current[n]]
    out = {'config': best, 'changes': changes, 'watts': saving,
           'watts_low': float(min(low, saving)), 'watts_high': float(max(high, saving)),
           'chance': {n: chance(float((phi(dict(best, **{n: current[n]})) - phi(best)) @ shrunk),
                                phi(dict(best, **{n: current[n]})) - phi(best)) for n in changes},
           'stable': sum(p == best for p in picks) / len(picks) if picks else 1.0}
    if energy and power:
        out.update(minutes=runtime_gain(energy, power, saving), low=runtime_gain(energy, power, out['watts_low']),
                   high=runtime_gain(energy, power, out['watts_high']))
    return out


# --- Quality of life --------------------------------------------------------------------

QOL_RISK = 0.1  # Acceptable chance per 4-minute block that a combination earns a 👎.
# Settings you might notice (experiment.catalog()'s `visible`), and the priors of
# the two vote models, (mean, sd) on the log-odds a setting's saving state adds:
# either vote starts at 2% per block; for 👎 a noticeable setting starts out
# multiplying the odds by ~2.5 (so experiments begin with about one at a time)
# and a quiet one at no effect; for 👍 every setting starts at no effect, either way.
NOTICEABLE = {'abm', 'refresh', 'boost', 'profile', 'wifi-ps', 'gpu-dpm', 'cpu-cap', 'throttle-apps'}
VOTE_BASE = 0.02
VOTE_PRIOR = {'down': {'quiet': (0.0, 1.0), 'noticeable': (0.9, 2.0)},
              'up': {'quiet': (0.0, 1.5), 'noticeable': (0.0, 2.0)}}


def qol_windows(minutes, events):
    """(setting states, {vote: given}, in an experiment) per 4-minute window —
    an experiment block, or a stretch outside experiments — that you were there
    for. A vote counts once, in the window of the minute it was cast in: a
    block and a stretch outside experiments can overlap in time, and 2 of the
    first 6 👎 counted in both."""
    top = tops(minutes)
    windows, window_of = {}, {}
    for m in minutes:
        if m.get('qol') is None:
            continue  # Before votes were logged.
        exp = m.get('exp')
        levers = [n for n in str(exp[1]).split('+') if n in SETTING] if exp else []
        key = (exp[0], exp[2]) if levers else ('-', int(m['t'] // 240))
        w = windows.setdefault(key, {'present': 0.0, 'n': 0, 'states': collections.Counter(), 'votes': set(),
                                     'experiment': bool(levers)})
        w['n'] += 1
        w['present'] += m.get('present', 0)
        w['states'][tuple(lever_states(m['set'], top, experiment_values(exp, levers) if levers else None).items())] += 1
        window_of[store.minute_of(m['t'])] = key
    for e in events:
        if e.get('event') == 'qol' and e.get('vote') in VOTE_PRIOR:
            # The vote's record ends up to a record (10 s) later, maybe in the next minute.
            key = window_of.get(store.minute_of(e['t1']), window_of.get(store.minute_of(e['t1'] + power.RECORD)))
            if key is not None:
                windows[key]['votes'].add(e['vote'])
    out = []
    for w in windows.values():
        if w['present'] / w['n'] < 0.5:
            continue  # Nobody there to notice.
        out.append((dict(w['states'].most_common(1)[0][0]), {v: v in w['votes'] for v in VOTE_PRIOR}, w['experiment']))
    return out


def vote_model(windows, vote, noticeable=NOTICEABLE):
    """Logistic: log-odds of a `vote` per window = intercept + `experiment`
    inside an experiment block (being switched around may itself earn votes)
    + the effect of each setting in its saving state; maximum a posteriori
    under VOTE_PRIOR, so it starts from the priors. Effects can be either sign:
    a setting's normal state can be the especially good one."""
    names = list(SETTING)
    priors = [(math.log(VOTE_BASE / (1 - VOTE_BASE)), 1.5), VOTE_PRIOR_EXPERIMENT] + [
        VOTE_PRIOR[vote]['noticeable' if n in noticeable else 'quiet'] for n in names]
    mean, sd = np.array([m for m, _ in priors]), np.array([s for _, s in priors])
    x = np.array([[1.0, float(w[2] if len(w) > 2 else True)] + [float(w[0][n]) for n in names]
                  for w in windows]).reshape(-1, 2 + len(names))
    y = np.array([float(w[1][vote]) for w in windows])

    def loss(theta):
        z = x @ theta
        nll = np.sum(np.logaddexp(0, z) - y * z) + np.sum((theta - mean) ** 2 / (2 * sd ** 2))
        return nll, x.T @ (expit(z) - y) + (theta - mean) / sd ** 2
    theta = minimize(loss, mean, jac=True, method='L-BFGS-B').x
    tally = {n: {side: {'votes': sum(1 for w in windows if w[0][n] == on and w[1][vote]),
                        'windows': sum(1 for w in windows if w[0][n] == on)}
                 for side, on in (('on', True), ('off', False))} for n in names}
    return {'intercept': float(theta[0]), 'experiment': float(theta[1]),
            'coef': {n: float(theta[2 + i]) for i, n in enumerate(names)},
            'windows': len(windows), 'votes': int(y.sum()), 'tally': tally}


VOTE_PRIOR_EXPERIMENT = (0.0, 1.0)  # The log-odds an experiment block adds, before any vote.


def qol_model(minutes, events, noticeable=NOTICEABLE):
    """Both vote models over the same windows: {'down': …, 'up': …}."""
    windows = qol_windows(minutes, events)
    return {vote: vote_model(windows, vote, noticeable) for vote in VOTE_PRIOR}


def vote_chance(model, config, experiment=True):
    """Chance per block of the model's vote with `config` ({name: saving state}),
    in an experiment block, or (`experiment` False) as your own settings."""
    return float(expit(model['intercept'] + (model.get('experiment', 0.0) if experiment else 0.0)
                       + sum(model['coef'][n] for n, on in config.items() if on and n in model['coef'])))


def qol_risk(models, config, experiment=True):
    """Chance per block that `config` earns a 👎."""
    return vote_chance(models['down'], config, experiment)


def delight(models, config, experiment=True):
    """Chance per block that `config` earns a 👍."""
    return vote_chance(models['up'], config, experiment)


# --- Automatic experiments: where to look next ------------------------------------------------

def features(config, names):
    pairs = itertools.combinations(names, 2)
    return [float(config[n]) for n in names] + [float(config[a] and config[b]) for a, b in pairs]


def plan(minutes, events, names, present=True, sigma=None, rng=None, noticeable=NOTICEABLE, group=None):
    """The next block's combination of `names` ({name: saving state}) for automatic experiments.

    The setting model's ridge fit is a Bayesian linear model (its penalties are
    the Gaussian prior), so its posterior is closed-form. As in lever_effects,
    each setting and pair comes from a fit without its MEDIATORS (load is a
    consequence of CPU settings, disk time of ASPM and APST), with that fit's
    block noise (block_sigma: run baselines and every setting that varied
    included, 1.56 W where the workload alone left 2.45); fits are taken as
    independent. Settings outside `names` that varied in earlier blocks get a
    main effect each, at their current state for the next block, so they
    neither add to the noise nor bias the others. Candidates are the
    combinations whose quality-of-life risk is acceptable (all of them while
    nobody is there). Each candidate's information is the expected reduction
    in the variance of the model's predictions across all candidates if it
    were observed next — Σ_c Cov(c, x)² / (σ² + Var x) per fit, a greedy
    integrated-variance design — and its cost (regret) the W it is expected
    to draw above the best candidate, over draws from the posterior. As in
    information-directed sampling, one is sampled in proportion to
    information / (cost + REGRET_FLOOR)², so blocks stay with combinations
    that might be best — experimenting costs little battery — while still
    sharpening the model. The next block shares a baseline with the earlier
    blocks of its `group` (block_group: the running run, today), so it is
    compared with them; a block in a new group (`group` None) only pins down
    its own baseline at first. `value`: what more testing is worth (testing_value).
    """
    rng = rng or np.random.default_rng()
    blocks = lever_blocks(minutes)
    group = group or 'next'
    groups = sorted({b['run'] for b in blocks} | {group})
    keys = [*names, *itertools.combinations(names, 2)]  # features()' order
    weights, y = block_weights(blocks), np.array([b['bat'] for b in blocks])
    qol = qol_model(minutes, events, noticeable)
    battery = [m for m in minutes if m['st'] == 'D' and m.get('set')]
    top = tops(minutes)
    now = lever_states(battery[-1]['set'], top) if battery else {}  # Settings outside `names`.
    configs = [dict(zip(names, combo)) for combo in itertools.product((False, True), repeat=len(names))]
    allowed = ([c for c in configs if not present or qol_risk(qol, dict(now, **c)) <= QOL_RISK]
               or [dict.fromkeys(names, False)])
    recent = [m for m in minutes[-30:] if m['st'] == 'D'] or minutes[-30:]
    contrast = np.array([features(c, names) for c in allowed]).reshape(len(allowed), len(keys))
    others = [n for n in SETTING if n not in names and len({b['x'][n] for b in blocks}) > 1]
    gain, mean, cov, sigmas = np.zeros(len(allowed)), np.zeros(len(keys)), np.zeros((len(keys), len(keys))), {}
    fits = []
    for fit in sorted({mediators(key) for key in keys}, key=sorted):
        covariates = tuple(c for c in COVARIATES if c not in fit)
        own = np.array([mediators(key) == fit for key in keys])
        s = sigmas[fit] = sigma or block_sigma(blocks, covariates) or 2.0
        k = len(groups) + len(covariates) + len(others)
        rows = np.hstack([lever_rows(blocks, groups, covariates, others),
                          np.array([features(b['x'], names) for b in blocks]).reshape(len(blocks), len(keys))])
        prior_sd = ([100.0] * len(groups) + [10.0] * len(covariates) + [PRIOR_SD['main']] * (len(others) + len(names))
                    + [PRIOR_SD['pair']] * (len(keys) - len(names)))
        precision = rows.T @ (rows * weights[:, None]) / s ** 2 + np.diag(1 / np.array(prior_sd) ** 2)
        post = np.linalg.inv(precision)
        post = (post + post.T) / 2
        beta = post @ (rows.T @ (weights * y)) / s ** 2
        workload = [float(np.mean([m.get(c) or 0 for m in recent])) if recent else 0.0 for c in covariates]
        # A prediction of interest is a contrast (no baseline) of this fit's features;
        # an observation includes today's baseline.
        mine = np.hstack([np.zeros((len(allowed), k)), contrast * own])
        context = [float(g == group) for g in groups] + workload + [float(now.get(n, False)) for n in others]
        observed = np.hstack([np.tile(context, (len(allowed), 1)), contrast])
        cross = mine @ post @ observed.T
        gain += (cross ** 2).sum(axis=0) / (s ** 2 + np.einsum('ij,jk,ik->i', observed, post, observed))
        mean[own] = beta[k:][own]
        cov[np.ix_(own, own)] = post[k:, k:][np.ix_(own, own)]
        fits.append((own, k, s, precision, observed))
    # W each candidate draws above every setting normal, per posterior draw.
    draws = rng.multivariate_normal(mean, cov, size=REGRET_DRAWS, method='eigh') @ contrast.T
    regret = (draws - draws.min(axis=1, keepdims=True)).mean(axis=0)
    score = gain / (regret + REGRET_FLOOR) ** 2
    chance = score / score.sum() if score.sum() > 0 else np.full(len(allowed), 1 / len(allowed))
    index = rng.choice(len(allowed), p=chance)
    spread = np.sqrt(np.maximum(np.einsum('ij,jk,ik->i', contrast, cov, contrast), 0))
    yours = {n: bool(own_settings(minutes, top).get(n, False)) for n in names}
    days = min(HORIZON_DAYS, max(1.0, (minutes[-1]['t'] - minutes[0]['t']) / 86400)) if minutes else 1.0
    on_battery = sum(m.get('dt', 60) for m in minutes if m['st'] == 'D') / 3600 / days
    return {'choice': allowed[index], 'cost': float(regret[index]), 'allowed': len(allowed), 'combinations': len(configs),
            'accuracy': float(1.645 * spread.mean()) if len(spread) else None,
            'least_certain': allowed[int(np.argmax(spread))] if len(spread) else None, 'qol': qol,
            'sigma': sigmas.get(frozenset(), next(iter(sigmas.values()), None)),
            'value': testing_value(fits, mean, cov, names, allowed, gain, yours, on_battery)}


HORIZON_DAYS = 30  # What a block teaches counts this long: every model fits the last 30 days (eta.WINDOW_DAYS).
TEST_HOURS = (10, 30, 100)  # More testing testing_value prices.
VALUE_DRAWS = 640_000  # Its posterior draws times combinations: 10000 draws for a pool of six settings.


def cautious_choice(m, cov, options, yours, singles, flips, confidence=CONFIDENCE):
    """The option recommendations would choose (best_config, approximately) were
    the effects' posterior N(m, cov), for each column of `m`: from your settings
    (options[yours]) only changes that save with `confidence` each on its own,
    Benjamini-Hochberg over the settings (`singles`: your settings with one
    changed), then the lowest-draw option that changes nothing else (`flips`:
    which settings each option changes). Rows of `options` and `singles` are
    features()."""
    pred = options @ m
    diff = singles - options[yours]
    sd = np.sqrt(np.maximum(np.einsum('ij,jk,ik->i', diff, cov, diff), 1e-12))
    p = ndtr(diff @ m / sd[:, None])  # The chance each change does not save.
    order, n = np.argsort(p, axis=0), len(singles)
    passing = np.take_along_axis(p, order, axis=0) <= (1 - confidence) * np.arange(1, n + 1)[:, None] / n
    found = np.where(passing.any(axis=0), n - np.argmax(passing[::-1], axis=0), 0)
    eligible = np.argsort(order, axis=0) < found
    feasible = flips @ (~eligible).astype(float) == 0
    return np.where(feasible, pred, np.inf).argmin(axis=0)


def testing_value(fits, mean, cov, names, allowed, gain, yours, hours_per_day, hours=TEST_HOURS,
                  draws=None, seed=0):
    """What more automatic testing is worth, in Wh of battery over the next
    HORIZON_DAYS: the battery time a block's information is used for before it
    leaves every model's window.

    What it improves is the choice you get: the recommendation, which changes
    your settings only where a change surely saves (cautious_choice). `loss` is
    the W that choice is expected to draw above the best of the combinations,
    under plan()'s posterior (`fits`: its fits' precision and candidate rows,
    `mean` and `cov` the effects'). After h more hours of blocks drawn as plan()
    draws them (from `gain` over `allowed`; the regret from these draws, not
    plan()'s fewer), the posterior covariance is known
    (a linear Gaussian model) and its mean moves by the difference, so the
    choice then and its loss are simulated together. h hours save
    (horizon - h) × (loss now - loss then) and cost h × the W blocks draw
    above the choice now (`cost`; under 0 when they draw less). `worth`: some
    h saves more than it costs, or too little battery time is logged to judge
    (no `rows`: none of `hours` fits in the horizon). Approximations: one baseline for all of those
    blocks (each day has its own), and plan()'s fits taken as independent."""
    keys = [*names, *itertools.combinations(names, 2)]
    options = list(allowed) + ([] if yours in allowed else [yours])
    x = np.array([features(c, names) for c in options]).reshape(len(options), len(keys))
    you = options.index(yours)
    singles = np.array([features(dict(yours, **{n: not yours[n]}), names) for n in names]).reshape(len(names), -1)
    flips = np.array([[float(c[n] != yours[n]) for n in names] for c in options])
    horizon = hours_per_day * HORIZON_DAYS
    draws = draws or max(1000, VALUE_DRAWS // len(options))
    z = np.random.default_rng(seed).standard_normal((2, len(keys), draws))

    def root(s):
        w, v = np.linalg.eigh((s + s.T) / 2)
        return v * np.sqrt(np.clip(w, 0, None))

    def loss(later):
        centre = mean[:, None] + root(cov - later) @ z[0]
        pred = x @ (centre + root(later) @ z[1])
        chosen = cautious_choice(centre, later, x, you, singles, flips)
        return float((pred[chosen, np.arange(draws)] - pred.min(axis=0)).mean())

    pred = x[:len(allowed)] @ (mean[:, None] + root(cov) @ z[1])
    score = gain / ((pred - pred.min(axis=0)).mean(axis=1) + REGRET_FLOOR) ** 2
    chance = score / score.sum() if score.sum() > 0 else np.full(len(allowed), 1 / len(allowed))
    choice = int(cautious_choice(mean[:, None], cov, x, you, singles, flips)[0])
    now, cost = loss(cov), float(chance @ (x[:len(allowed)] @ mean) - x[choice] @ mean)
    rows = []
    for h in (h for h in hours if h < horizon):
        later = np.zeros_like(cov)
        for own, k, s, precision, observed in fits:
            added = h * 60 / BLOCK_MINUTES * (observed * chance[:, None]).T @ observed / s ** 2
            post = np.linalg.inv(precision + added)
            later[np.ix_(own, own)] = post[k:, k:][np.ix_(own, own)]
        then = loss(later)
        rows.append({'hours': h, 'loss': then, 'saves': (horizon - h) * (now - then), 'costs': h * cost})
    return {'horizon_days': HORIZON_DAYS, 'horizon_hours': horizon, 'loss': now, 'cost': cost,
            'choice': options[choice], 'rows': rows,
            'worth': not rows or any(r['saves'] > r['costs'] for r in rows)}


REGRET_DRAWS = 300  # Posterior draws for each candidate's expected cost.
REGRET_FLOOR = 0.3  # W: keeps a candidate that is surely best from taking every block.


# How to say a setting is in its (power-saving, normal) state.
PHRASES = {'aspm': ('ASPM on', 'ASPM off'), 'apst': ('APST on', 'APST off'),
           'wifi-ps': ('Wi-Fi power saving on', 'Wi-Fi power saving off'), 'abm': ('ABM on', 'ABM off'),
           'refresh': ('lower refresh rate', 'full refresh rate'), 'boost': ('CPU boost off', 'CPU boost on'),
           'profile': ('power-saver profile', 'balanced profile'),
           'gpu-dpm': ('GPU clocks low', 'GPU clocks automatic'), 'cpu-cap': ('CPU capped at 2 GHz', 'CPU uncapped'),
           'throttle-apps': ('background apps throttled', 'background apps unthrottled')}
GOALS = ('battery', 'comfort')


def gain_parts(gain):
    """('+14 min', '−8 … +51'): minutes per full charge and the 90% interval ('' when unknown)."""
    if not gain or gain.get('minutes') is None:
        return '', ''
    if not math.isfinite(gain['minutes']):
        return 'unbounded', ''
    signed = lambda minutes: f'{minutes:+.0f}'.replace('-', '−')
    low, high = gain.get('low'), gain.get('high')
    bounded = low is not None and high is not None and math.isfinite(low) and math.isfinite(high)
    return f"{signed(gain['minutes'])} min", f'{signed(low)} … {signed(high)}' if bounded else ''


def gain_text(gain):
    """'+14 min (−8 … +51)': minutes per full charge and the 90% interval."""
    minutes, interval = gain_parts(gain)
    return f'{minutes} ({interval})' if interval else minutes


def recommendations(levers, quality, energy=None, power=None, exclude=()):
    """The best measured combination for each goal (best_config: only changes
    that surely save), among those unlikely to earn a 👎 as your settings, and
    leaving `exclude` (e.g. experiment.RISKY, or settings outside the pool) as
    they are: 'battery' the lowest draw, 'comfort' the likeliest 👍 (to a
    percentage point; then Battery's changes; Battery's until a 👍 exists to
    learn from). Each: the combination, `apply` (power-experiment apply
    arguments for all of it), what it changes from your settings (`change`,
    with `gain` and `interval` per full charge, and for comfort `delight`, the
    👍 chance per block now → then) and all of it as `text`."""
    measured = {e['experiment'] for e in levers['effects']}
    fine = lambda c: qol_risk(quality, c, experiment=False) <= QOL_RISK
    learned = bool(quality['up']['votes'])
    keys = {'battery': None,
            'comfort': (lambda c: -round(delight(quality, c, experiment=False), 2)) if learned else None}
    out = {}
    for goal, key in keys.items():
        best = best_config(levers, measured, energy, power, feasible=fine, key=key, exclude=exclude)
        entry = {'best': best, 'apply': [], 'change': 'nothing measured yet', 'gain': '', 'interval': '',
                 'delight': ''}
        if best:  # Every setting in it: applying must hold wherever the settings are now.
            entry['apply'] = [f"{n}={'saving' if on else 'normal'}" for n, on in best['config'].items()]
            # Non-breaking inside each setting, so wrapped lines break only between settings.
            entry['change'] = ', '.join(PHRASES.get(n, (n, n))[0 if best['config'][n] else 1].replace(' ', '\u00a0')
                                        for n in best['changes']) or 'already set'
            if best['changes']:
                entry['gain'], entry['interval'] = gain_parts(best)
            if goal == 'comfort' and not learned:
                entry['change'] = 'same as Battery until a 👍'
            elif goal == 'comfort' and best['changes']:
                now, then = (delight(quality, c, experiment=False)
                             for c in (levers['current'], {**levers['current'], **best['config']}))
                entry['delight'] = f'{100 * now:.0f}% → {100 * then:.0f}%'
        entry['text'] = ' · '.join(filter(None, [entry['change'], entry['delight'] and f"👍 {entry['delight']}",
                                                 best and best['changes'] and gain_text(best)]))
        out[goal] = entry
    return out


# --- Sleep ----------------------------------------------------------------------

SLEEP_MAX_W = 3.0  # Suspended drain is ~1 W: more means the collector stopped while awake.
SLEEP_NEAR = 300  # s: how far from a gap the minute that says it was on battery may be.
SLEEP_SHARE = 0.9  # Of a gap that must have been suspended, when the event says (`suspended_s`).


def sleep_drain(events, minutes=None):
    """Suspends (gap events) with battery drain: start, hours, W, %/h.

    A gap is any stall of the collector, not only a suspend (2026-10-05: 0.7 h
    at 7.8 W, awake), so one draining SLEEP_MAX_W or more is left out, and so
    is one whose event says it was mostly awake: `suspended_s`, the time the
    boot clock ran ahead of the monotonic one (which stops in suspend), under
    SLEEP_SHARE of the gap. With `minutes`, a gap counts only if the minutes
    on both sides were on battery (one touching AC charged or ran on it for
    part of the time)."""
    times = [m['t'] for m in minutes] if minutes else None

    def status(t, after):
        i = bisect.bisect_left(times, t) if after else bisect.bisect_right(times, t) - 1
        if 0 <= i < len(times) and abs(times[i] - t) <= SLEEP_NEAR:
            return minutes[i]['st']
        return None
    result = []
    for event in events:
        if event.get('event') != 'gap':
            continue
        before, after, gap = event.get('bat_before') or {}, event.get('bat_after') or {}, event.get('gap_s', 0)
        if event.get('suspended_s') is not None and event['suspended_s'] < SLEEP_SHARE * gap:
            continue  # Awake for part of it.
        hours = gap / 3600
        if hours < 0.25 or before.get('charge') is None or after.get('charge') is None:
            continue
        drop = before['charge'] - after['charge']
        if drop < 0:
            continue  # Charged while asleep.
        start = event['t1'] - gap
        if times is not None and not (status(start, False) == 'D' == status(store.minute_of(event['t1']), True)):
            continue
        volts = before.get('volts') or after.get('volts') or 15.4
        wh = drop / 1e6 if before.get('unit') == 'uWh' else drop / 1e6 * volts
        if wh / hours >= SLEEP_MAX_W:
            continue
        result.append({'start': start, 'end': event['t1'], 'hours': hours, 'watts': wh / hours,
                       'pct_per_hour': ((before.get('pct') or 0) - (after.get('pct') or 0)) / hours,
                       'pct_before': before.get('pct'), 'pct_after': after.get('pct')})
    return result


def weighted_median(values, weights):
    order = np.argsort(values)
    cumulative = np.cumsum(np.asarray(weights, float)[order])
    return float(np.asarray(values)[order][np.searchsorted(cumulative, cumulative[-1] / 2)])


def sleep_model(sleeps):
    """Log-normal fit of per-suspend drain: (log centre, predictive σ, suspends) or None.

    Drain is always positive and a few bad nights skew high; longer suspends
    weigh more (short ones are mostly counter rounding). Centre and spread are
    robust — the weighted median and MAD of the logs — so one odd suspend
    doesn't move them. σ is predictive for one new suspend: the spread between
    suspends plus the uncertainty of their median (π/2 × that of a mean).
    """
    rows = [(s['watts'], s['hours']) for s in sleeps if s['watts'] > 0.05 and s['hours'] >= 0.25]
    if len(rows) < 3:
        return None
    logs = np.log([w for w, _ in rows])
    weights = np.sqrt([h for _, h in rows])
    centre = weighted_median(logs, weights)
    spread = max(1.4826 * weighted_median(np.abs(logs - centre), weights), 0.05) * math.sqrt(len(rows) / (len(rows) - 1))
    effective = weights.sum() ** 2 / (weights ** 2).sum()
    return centre, spread * math.sqrt(1 + math.pi / 2 / effective), len(rows)


def sleep_runtime(sleeps, energy_wh, z=1.2816, model=None):
    """Hours a suspend would last from `energy_wh`, with an 80% interval."""
    model = model or sleep_model(sleeps)
    if model is None or not energy_wh:
        return None
    mean, sigma, count = model
    return {'hours': energy_wh / math.exp(mean), 'low': energy_wh / math.exp(mean + z * sigma),
            'high': energy_wh / math.exp(mean - z * sigma), 'watts': math.exp(mean), 'suspends': count}


# --- Timeline --------------------------------------------------------------------

def timeline(minutes, model):
    """Per-minute stacks: [{'t', 'st', 'groups', 'apps', 'devices', 'idle', 'bat', 'pct'}]."""
    out = []
    for m in minutes:
        parts = attribute(m, model)
        out.append({'t': m['t'], 'st': m['st'], 'groups': dict(parts['groups']), 'apps': dict(parts['apps']),
                    'devices': parts['devices'], 'idle': parts['idle'], 'bat': m.get('bat'), 'soc': m.get('soc'),
                    'pct': m.get('pct'), 'bl': m.get('bl'), 'exp': m.get('exp')})
    return out
