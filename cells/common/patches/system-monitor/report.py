"""Battery history analysis for the battery panel.

Per-minute rows from the power database (store.py, written by the collector),
then:

* battery-only models: chip W ~ floor + load (busy cores × GHz²) + GPU + video,
  and battery W ~ base + k·chip + backlight + radio/storage/USB/audio/keyboard,
  fitted by non-negative least squares on one-minute means (the battery reading
  lags chip power by ~10 s);
* per-minute attribution into stable groups for the stacked timeline;
* what-if runtime gains: model-based (marginal effect of removing an app's
  activity or dimming) with moving-block bootstrap intervals, and experiment-
  based (paired A/B blocks from power-experiment) with pair-bootstrap intervals;
* sleep drain per suspend from the log's gap events.
"""
import bisect
import collections
import datetime
import itertools
import math
import os
import re
from pathlib import Path

# Before numpy: these are small matrices, and BLAS threads only contend for them
# (a 226×76 least squares took 27 ms with 12 threads, 4 ms with one).
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

import numpy as np  # noqa: E402
from scipy.optimize import minimize, nnls  # noqa: E402
from scipy.special import expit  # noqa: E402

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
CATEGORIES = ['Processor baseline', 'Rest of system', 'Display'] + APP_GROUPS
DEVICES = (('Display', 'bl'), ('Wi-Fi', 'wifi'), ('Storage', 'disk'), ('USB devices', 'usb'),
           ('Audio', 'audio'), ('Keyboard backlight', 'kbd'))
CHIP_TERMS = ('floor', 'load', 'gpu', 'video')
BATTERY_TERMS = ('base', 'chip', 'bl', 'wifi', 'disk', 'usb', 'audio', 'kbd')
PRIOR = power.PRIOR  # Shared with the collector's attribution.


def display_name(app):
    """Undo /proc/PID/comm truncation artifacts like 'openroad-wrapp'."""
    return re.sub(r'-w(?:r(?:a(?:p(?:p(?:e(?:d)?)?)?)?)?)?$|\.bin$', '', app)


def group_of(app):
    for group, prefixes in GROUPS:
        if app.startswith(prefixes):
            return group
    return 'Other apps'


def load_range(start, end, db=None):
    """Per-minute rows and events with start <= t < end, from the power database."""
    return store.load_range(start, end, db or store.DB)


# --- Models -------------------------------------------------------------------

def battery_rows(minutes, include_experiments=False):
    return [m for m in minutes if m['st'] == 'D' and m.get('bat', 0) > 0 and m.get('soc') is not None
            and m.get('load') is not None and m['dt'] >= 40 and (include_experiments or 'exp' not in m)
            and m['bat'] - m['soc'] > -2]


def design(rows):
    chip = np.array([[1.0, m['load'], m.get('gpu', 0), m.get('video', 0)] for m in rows])
    battery = np.array([[1.0, m['soc'], m.get('bl') or 0, m.get('wifi', 0), m.get('disk', 0),
                         m.get('usb', 0), m.get('audio', 0), m.get('kbd') or 0] for m in rows])
    return chip, np.array([m['soc'] for m in rows]), battery, np.array([m['bat'] for m in rows])


def fit(chip_x, chip_y, battery_x, battery_y, ridge=5.0):
    """Non-negative least squares, lightly shrunk toward PRIOR so coefficients of
    features that never varied stay at their prior instead of drifting."""
    def solve(x, y, names):
        prior = np.array([PRIOR[n] for n in names])
        penalty = math.sqrt(ridge) * np.eye(len(names))
        penalty[0, 0] = 0.1  # Intercepts: barely shrunk.
        coef, _ = nnls(np.vstack([x, penalty]), np.concatenate([y, penalty @ prior]))
        return dict(zip(names, coef))
    model = {}
    if len(chip_y) >= 20:
        model.update(solve(chip_x, chip_y, CHIP_TERMS))
        model.update(solve(battery_x, battery_y, BATTERY_TERMS))
    else:
        model.update(PRIOR)
    return model


def fit_rows(rows):
    return fit(*design(rows)) if rows else dict(PRIOR)


def attribute(m, model):
    """Watts for one minute: {'groups': {category: W}, 'apps': {app: W}, 'devices': {device: W}}."""
    soc = m.get('soc')
    groups, apps, devices = collections.defaultdict(float), collections.defaultdict(float), {}
    if soc is None:
        return {'groups': groups, 'apps': apps, 'devices': devices}
    on_battery = m['st'] == 'D' and m.get('bat') is not None
    k = model['chip'] if on_battery else 1.0
    floor = min(soc, model['floor'])
    weights = {}
    owned = 0.0
    for name, (load, gpu, video) in m.get('apps', {}).items():
        weights[name] = model['load'] * load + model['gpu'] * gpu + model['video'] * video
        owned += load
    if m.get('load') is not None:
        weights['Kernel'] = weights.get('Kernel', 0) + model['load'] * max(0.0, m['load'] - owned)
    total = sum(weights.values())
    if total <= 0:
        floor = soc
    for name, weight in weights.items():
        if weight > 0:
            watts = k * (soc - floor) * weight / total
            apps[name] += watts
            groups[group_of(name)] += watts
    groups['Processor baseline'] += k * floor
    if on_battery:
        rest = m['bat'] - k * soc
        parts = {name: model[term] * (m.get(term) or 0) for name, term in DEVICES}
        modelled = sum(parts.values())
        shrink = min(1.0, max(0.0, rest) / modelled) if modelled > 0 else 0.0
        devices = {name: watts * shrink for name, watts in parts.items() if watts * shrink > 0.005}
        groups['Display'] += devices.get('Display', 0)
        groups['Rest of system'] += max(0.0, rest - modelled) + sum(w for n, w in devices.items() if n != 'Display')
    return {'groups': groups, 'apps': apps, 'devices': devices}


# --- What-if runtime ------------------------------------------------------------

def full_energy(minutes, sys=Path('/sys')):
    """Usable Wh of a full charge: charge_full × mean discharge voltage (or energy_full)."""
    volts = [m['volts'] for m in minutes if m['st'] == 'D' and m.get('volts')]
    for battery in sorted((sys / 'class/power_supply').glob('BAT*')):
        try:
            if (battery / 'energy_full').exists():
                return float((battery / 'energy_full').read_text()) / 1e6
            charge = float((battery / 'charge_full').read_text()) / 1e6
            return charge * (sum(volts) / len(volts) if volts else 15.4)
        except (OSError, ValueError):
            continue
    return None


def runtime_gain(energy, power, saved):
    """Minutes of extra runtime from a full charge when average draw drops by `saved` W."""
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

    def activity(names):
        columns = np.zeros((len(rows), 3))
        for i, m in enumerate(rows):
            for name, values in m.get('apps', {}).items():
                if name in names:
                    columns[i] += values
        return lambda c: c['chip'] * (c['load'] * columns[:, 0] + c['gpu'] * columns[:, 1] + c['video'] * columns[:, 2])
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


def what_if(rows, energy, replicates=200, block=10, seed=0, model=None, train_rows=None):
    """Point estimate and 90% interval of runtime gain (minutes) per intervention.

    `model` is the shared trained model (battery-eta's models.json), evaluated
    on this period's `rows`; without one, the models are fitted on
    `train_rows`. The interval refits both models on moving-block bootstrap
    resamples of the training rows (10-minute blocks: residuals stay correlated
    for minutes) and averages over block resamples of the period, so it covers
    coefficient uncertainty as well as the period's variability.
    """
    train_rows = train_rows or rows
    if len(rows) < 30 or len(train_rows) < 30 or not energy:
        return [], None
    train = design(train_rows)
    model = model or fit(*train)
    _, _, _, battery_y = design(rows)
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
    for _ in range(replicates):
        picked = block_indices(len(train_rows), block, rng)
        coef = fit(*(part[picked] for part in train))
        for name, gain in evaluate(block_indices(len(rows), block, rng), coef)[1].items():
            samples[name].append(gain)
    estimates = []
    for name, gain in point.items():
        low, high = np.percentile(samples[name], [5, 95])
        estimates.append({'name': name, 'evidence': 'model', 'watts': saved[name], 'minutes': gain,
                          'low': float(min(low, gain)), 'high': float(max(high, gain))})
    return (sorted(estimates, key=lambda e: -e['minutes']),
            {'power': power, 'energy': energy, 'model': model, 'runtime_h': energy / power, 'rows': len(rows)})


# --- Experiments ------------------------------------------------------------------

def experiment_blocks(minutes):
    blocks = collections.defaultdict(lambda: [0.0, 0.0, None, None, None])
    for m in minutes:
        exp = m.get('exp')
        if not exp or exp[4] or m['st'] != 'D' or m.get('bat') is None:
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


def experiment_effects(minutes, energy, power, replicates=2000, seed=0):
    """Paired consecutive A/B blocks per run → saving of arm B (W) and runtime gain.
    Only runs of one setting (app freezing): a combined run's arm is a string
    of letters and its value a dict per setting, which lever_effects fits."""
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
            boot = rng.choice(deltas, size=(replicates, len(deltas))).mean(axis=1)
            low, high = np.percentile(boot, [5, 95])
            estimate.update(minutes=runtime_gain(energy, power, estimate['watts']),
                            low=runtime_gain(energy, power, float(low)),
                            high=runtime_gain(energy, power, float(high)),
                            watts_low=float(low), watts_high=float(high))
        results.append(estimate)
    return results


def boot_effect(minutes, energy, power, key='psr', replicates=1000, seed=0, min_boots=2, min_rows=30):
    """Boot-level setting effect on battery draw, adjusted for workload.

    Least squares of battery W on load, GPU/video, backlight, Wi-Fi and disk plus
    an indicator for the setting; the interval resamples whole boots within
    each arm (minutes inside a boot are not independent). Returns progress
    counts until each arm has `min_boots` boots with battery time.
    """
    rows = [m for m in battery_rows(minutes) if m.get('boot')]
    arms = {False: collections.defaultdict(list), True: collections.defaultdict(list)}
    for m in rows:
        arms[bool(m['set'].get(key))][m['boot']].append(m)
    progress = {'experiment': key, 'evidence': 'boot', 'value': 'on',
                'boots': {'off': len(arms[False]), 'on': len(arms[True])},
                'hours': {'off': sum(len(v) for v in arms[False].values()) / 60,
                          'on': sum(len(v) for v in arms[True].values()) / 60}}
    enough = lambda arm: sum(1 for v in arm.values() if len(v) >= 10) >= min_boots
    if not (enough(arms[False]) and enough(arms[True])) or len(rows) < 2 * min_rows:
        return progress

    def sums(boot, arm):  # Each boot's XᵀX and Xᵀy: a resample only adds them up.
        x = np.array([[1.0, m['load'], m.get('gpu', 0), m.get('video', 0), m.get('bl') or 0,
                       m.get('wifi', 0), m.get('disk', 0), float(arm)] for m in boot])
        return x.T @ x, x.T @ np.array([m['bat'] for m in boot])
    groups = {arm: [sums(boot, arm) for boot in boots.values()] for arm, boots in arms.items()}

    def estimate(picked):
        xx = sum(xx for arm in picked for xx, _ in picked[arm])
        xy = sum(xy for arm in picked for _, xy in picked[arm])
        coef = np.linalg.lstsq(xx, xy, rcond=None)[0]  # = least squares on the rows, rank-deficient or not.
        return -float(coef[-1])  # Watts saved with the setting on.
    saved = estimate(groups)
    rng = np.random.default_rng(seed)
    boot_samples = []
    for _ in range(replicates):
        resample = {arm: [boots[i] for i in rng.integers(0, len(boots), len(boots))] for arm, boots in groups.items()}
        boot_samples.append(estimate(resample))
    low, high = np.percentile(boot_samples, [5, 95])
    progress.update(watts=saved, watts_low=float(low), watts_high=float(high))
    if energy and power:
        progress.update(minutes=runtime_gain(energy, power, saved), low=runtime_gain(energy, power, float(low)),
                        high=runtime_gain(energy, power, float(high)))
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
    return states


def own_settings(minutes, top=None):
    """Your settings, as saving states: the latest minute's outside experiments
    (the runner puts them back), except AC_MANAGED ones, which come from the
    latest such minute on battery."""
    top = top or tops(minutes)
    logged = [m for m in minutes if m.get('set')]
    own = [m for m in logged if not m.get('exp')] or logged
    states = lever_states(own[-1]['set'] if own else {}, top)
    battery = [m for m in own if m['st'] == 'D']
    if battery:
        states.update({n: on for n, on in lever_states(battery[-1]['set'], top).items() if n in AC_MANAGED})
    return states


# udev switches the profile and ABM on AC/battery changes, and each profile
# change sets boost (power-profiles-daemon: off for power-saver).
AC_MANAGED = {'profile', 'abm', 'boost'}


def experiment_values(exp, levers):
    """The values a minute's experiment set: a dict for several settings, a value for one."""
    return exp[5] if isinstance(exp[5], dict) else {levers[0]: exp[5]} if len(levers) == 1 else {}


def tops(minutes):
    """The highest refresh rate and CPU frequency cap logged: their normal states."""
    return {key: max((m['set'][key] for m in minutes if (m.get('set') or {}).get(key)), default=None)
            for key in ('hz', 'mhz', 'cpu_mhz')}


def block_group(run, t):
    """A block's baseline group: its run and day (an automatic run goes on for weeks)."""
    return f"{run}@{datetime.date.fromtimestamp(t).isoformat()}"


def lever_blocks(minutes):
    """Clean blocks of setting experiments (single or combined): battery W and
    workload means, and which settings were in their saving state."""
    top = tops(minutes)
    acc = {}
    for m in minutes:
        exp = m.get('exp')
        if not exp or exp[4] or m['st'] != 'D' or m.get('bat') is None or m.get('load') is None or m['dt'] < 40:
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


def lever_effects(minutes, energy, power, replicates=300, block=3, seed=0, min_blocks=3):
    """Savings of each experimented setting, and of pairs of them together.

    One regression over clean blocks of every setting experiment: battery W on
    a baseline per run (so a setting is only ever compared within the run that
    randomized it), workload, an indicator per setting (saving state) and per
    pair of settings randomized together (shrunk toward zero: most pairs don't
    interact). CPU settings come from a second fit without the CPU-load
    correction. Effects are for your settings (own_settings: the latest
    battery minute outside experiments). Intervals: moving-block bootstrap over consecutive blocks.
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
    covariates = {False: COVARIATES, True: tuple(c for c in COVARIATES if c != 'load')}
    if not shown or len(blocks) < len(runs) + len(COVARIATES) + len(varying) + len(pairs) + 5:
        return result
    y = np.array([b['bat'] for b in blocks])
    w = np.array([b['seconds'] for b in blocks])
    w = w / w.mean()

    def design(cpu):
        return np.array([[*(float(b['run'] == run) for run in runs), *(b[c] for c in covariates[cpu]),
                          *(float(b['x'][n]) for n in varying), *(float(b['x'][a] and b['x'][c]) for a, c in pairs)]
                         for b in blocks])
    x = {cpu: design(cpu) for cpu in (False, True)}

    def noise(cpu):  # Residual variance of the main-effects fit, per block.
        k = x[cpu].shape[1] - len(pairs)
        coef = ridge(x[cpu][:, :k], y, w, np.full(k, 1e-6))
        resid = y - x[cpu][:, :k] @ coef
        return float(np.sum(w * resid ** 2) / max(1, len(y) - k))
    sigma2 = {cpu: noise(cpu) for cpu in (False, True)}

    def fit(index):
        # A resample as weights (how often each block was drawn): the same sums as
        # its rows, without copying them.
        counts = w * np.bincount(index, minlength=len(blocks))
        coef = {}
        for cpu in (False, True):
            base = len(runs) + len(covariates[cpu])
            penalty = np.array([1e-6] * base + [sigma2[cpu] / PRIOR_SD['main'] ** 2] * len(varying)
                               + [sigma2[cpu] / PRIOR_SD['pair'] ** 2] * len(pairs))
            beta = ridge(x[cpu], y, counts, penalty)
            for i, name in enumerate(varying):
                if (name in CPU_LEVERS) == cpu:
                    coef[name] = beta[base + i]
            for i, pair in enumerate(pairs):
                if bool(CPU_LEVERS & set(pair)) == cpu:
                    coef[pair] = beta[base + len(varying) + i]
        return coef
    coef = fit(np.arange(len(blocks)))
    rng = np.random.default_rng(seed)
    draws = [fit(block_indices(len(blocks), block, rng)) for _ in range(replicates)]
    samples = {key: np.array([d[key] for d in draws]) for key in coef}
    hours = lambda name: sum(b['seconds'] for b in blocks if name in b['levers']) / 3600

    def summary(saving, values):
        low, high = np.percentile(values, [5, 95])
        estimate = {'watts': float(saving), 'watts_low': float(low), 'watts_high': float(high)}
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
    result['interactions'] = [dict(summary(-coef[pair], -samples[pair]), pair=pair) for pair in pairs]
    result.update(coef=coef, samples=samples, sigma=math.sqrt(sigma2[False]), sigma_raw=math.sqrt(sigma2[True]))
    return result


def predicted(coef, config):
    """Battery W relative to every measured setting off, for `config` {name: on}."""
    return sum(value * (all(config.get(n) for n in key) if isinstance(key, tuple) else config.get(key, False))
               for key, value in coef.items())


def best_config(result, allowed, energy=None, power=None, feasible=None, key=None):
    """The measured combination of `allowed` settings (others stay as they are)
    among those `feasible` accepts with the lowest `key` — by default the
    predicted draw — and its battery saving against the current one."""
    names = [e['experiment'] for e in result['effects'] if e['experiment'] in allowed]
    if not names:
        return None
    current = dict(result['current'])
    configs = [dict(current, **dict(zip(names, combo))) for combo in itertools.product((False, True), repeat=len(names))]
    configs = [c for c in configs if feasible is None or feasible(c) or c == current]
    best = min(configs, key=key or (lambda c: predicted(result['coef'], c)))
    saving = predicted(result['coef'], current) - predicted(result['coef'], best)
    draws = predicted(result['samples'], current) - predicted(result['samples'], best)  # Every draw at once.
    low, high = np.percentile(draws, [5, 95])
    out = {'config': {n: best[n] for n in names}, 'changes': [n for n in names if best[n] != current.get(n)],
           'watts': saving, 'watts_low': float(low), 'watts_high': float(high)}
    if energy and power:
        out.update(minutes=runtime_gain(energy, power, saving), low=runtime_gain(energy, power, float(low)),
                   high=runtime_gain(energy, power, float(high)))
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


def vote_times(events, vote):
    return sorted(e['t1'] for e in events if e.get('event') == 'qol' and e.get('vote') == vote)


def qol_windows(minutes, events):
    """(setting states, {vote: given}) per 4-minute window — an experiment block,
    or a stretch outside experiments — that you were there for."""
    times, top = {v: vote_times(events, v) for v in VOTE_PRIOR}, tops(minutes)
    windows = {}
    for m in minutes:
        if m.get('qol') is None:
            continue  # Before votes were logged.
        exp = m.get('exp')
        levers = [n for n in str(exp[1]).split('+') if n in SETTING] if exp else []
        w = windows.setdefault((exp[0], exp[2]) if levers else ('-', int(m['t'] // 240)),
                               {'t0': m['t'], 'present': 0.0, 'n': 0, 'states': collections.Counter()})
        w['t1'], w['n'] = m['t'] + 60, w['n'] + 1
        w['present'] += m.get('present', 0)
        w['states'][tuple(lever_states(m['set'], top, experiment_values(exp, levers) if levers else None).items())] += 1
    out = []
    for w in windows.values():
        if w['present'] / w['n'] < 0.5:
            continue  # Nobody there to notice.
        given = {v: bisect.bisect_left(t, w['t0']) < bisect.bisect_left(t, w['t1']) for v, t in times.items()}
        out.append((dict(w['states'].most_common(1)[0][0]), given))
    return out


def vote_model(windows, vote, noticeable=NOTICEABLE):
    """Logistic: log-odds of a `vote` per window = intercept + the effect of each
    setting in its saving state; maximum a posteriori under VOTE_PRIOR, so it
    starts from the priors. Effects can be either sign: a setting's normal state
    can be the especially good one."""
    names = list(SETTING)
    priors = [(math.log(VOTE_BASE / (1 - VOTE_BASE)), 1.5)] + [
        VOTE_PRIOR[vote]['noticeable' if n in noticeable else 'quiet'] for n in names]
    mean, sd = np.array([m for m, _ in priors]), np.array([s for _, s in priors])
    x = np.array([[1.0] + [float(states[n]) for n in names] for states, _ in windows]).reshape(-1, 1 + len(names))
    y = np.array([float(given[vote]) for _, given in windows])

    def loss(theta):
        z = x @ theta
        nll = np.sum(np.logaddexp(0, z) - y * z) + np.sum((theta - mean) ** 2 / (2 * sd ** 2))
        return nll, x.T @ (expit(z) - y) + (theta - mean) / sd ** 2
    theta = minimize(loss, mean, jac=True, method='L-BFGS-B').x
    tally = {n: {side: {'votes': sum(1 for states, given in windows if states[n] == on and given[vote]),
                        'windows': sum(1 for states, _ in windows if states[n] == on)}
                 for side, on in (('on', True), ('off', False))} for n in names}
    return {'intercept': float(theta[0]), 'coef': {n: float(theta[1 + i]) for i, n in enumerate(names)},
            'windows': len(windows), 'votes': int(y.sum()), 'tally': tally}


def qol_model(minutes, events, noticeable=NOTICEABLE):
    """Both vote models over the same windows: {'down': …, 'up': …}."""
    windows = qol_windows(minutes, events)
    return {vote: vote_model(windows, vote, noticeable) for vote in VOTE_PRIOR}


def vote_chance(model, config):
    """Chance per block of the model's vote with `config` ({name: saving state})."""
    return float(expit(model['intercept'] + sum(model['coef'][n] for n, on in config.items()
                                                 if on and n in model['coef'])))


def qol_risk(models, config):
    """Chance per block that `config` earns a 👎."""
    return vote_chance(models['down'], config)


def delight(models, config):
    """Chance per block that `config` earns a 👍."""
    return vote_chance(models['up'], config)


# --- Automatic experiments: where to look next ------------------------------------------------

def features(config, names):
    pairs = itertools.combinations(names, 2)
    return [float(config[n]) for n in names] + [float(config[a] and config[b]) for a, b in pairs]


def plan(minutes, events, names, present=True, sigma=None, rng=None, noticeable=NOTICEABLE, group=None):
    """The next block's combination of `names` ({name: saving state}) for automatic experiments.

    The setting model's ridge fit is a Bayesian linear model (its penalties are
    the Gaussian prior), so its posterior is closed-form. Candidates are the
    combinations whose quality-of-life risk is acceptable (all of them while
    nobody is there). Each candidate's information is the expected reduction in
    the variance of the model's predictions across all candidates if it were
    observed next — Σ_c Cov(c, x)² / (σ² + Var x), a greedy integrated-variance
    design — and its cost (regret) the W it is expected to draw above the best
    candidate, over draws from the posterior. As in information-directed
    sampling, one is sampled in proportion to information / (cost +
    REGRET_FLOOR)², so blocks stay with combinations that might be best —
    experimenting costs little battery — while still sharpening the model.
    The next block shares a baseline with the earlier blocks of its `group`
    (block_group: the running run, today), so it is compared with them; a block
    in a new group (`group` None) only pins down its own baseline at first.
    """
    rng = rng or np.random.default_rng()
    blocks = lever_blocks(minutes)
    sigma = sigma or block_sigma(blocks) or 2.0
    group = group or 'next'
    groups = sorted({b['run'] for b in blocks} | {group})
    k = len(groups) + len(COVARIATES)
    rows = np.array([[*(float(b['run'] == g) for g in groups), *(b[c] for c in COVARIATES), *features(b['x'], names)]
                     for b in blocks]).reshape(-1, k + len(features(dict.fromkeys(names, False), names)))
    weights = np.array([b['seconds'] / 180 for b in blocks])  # A clean block is 3 minutes.
    prior_sd = ([100.0] * len(groups) + [10.0] * len(COVARIATES) + [PRIOR_SD['main']] * len(names)
                + [PRIOR_SD['pair']] * (len(names) * (len(names) - 1) // 2))
    precision = rows.T @ (rows * weights[:, None]) / sigma ** 2 + np.diag(1 / np.array(prior_sd) ** 2)
    cov = np.linalg.inv(precision)
    cov = (cov + cov.T) / 2
    mean = cov @ (rows.T @ (weights * np.array([b['bat'] for b in blocks]))) / sigma ** 2
    qol = qol_model(minutes, events, noticeable)
    battery = [m for m in minutes if m['st'] == 'D' and m.get('set')]
    now = lever_states(battery[-1]['set'], tops(minutes)) if battery else {}  # Settings outside `names`.
    configs = [dict(zip(names, combo)) for combo in itertools.product((False, True), repeat=len(names))]
    allowed = ([c for c in configs if not present or qol_risk(qol, dict(now, **c)) <= QOL_RISK]
               or [dict.fromkeys(names, False)])
    recent = [m for m in minutes[-30:] if m['st'] == 'D'] or minutes[-30:]
    workload = [float(np.mean([m.get(c) or 0 for m in recent])) if recent else 0.0 for c in COVARIATES]
    # A prediction of interest is a contrast (no baseline); an observation includes today's.
    contrast = np.array([[0.0] * k + features(c, names) for c in allowed])
    observed = np.array([[float(g == group) for g in groups] + workload + features(c, names) for c in allowed])
    cross = contrast @ cov @ observed.T
    gain = (cross ** 2).sum(axis=0) / (sigma ** 2 + np.einsum('ij,jk,ik->i', observed, cov, observed))
    # W each candidate draws above every setting normal, per posterior draw.
    draws = rng.multivariate_normal(mean[k:], cov[k:, k:], size=REGRET_DRAWS, method='eigh') @ contrast[:, k:].T
    regret = (draws - draws.min(axis=1, keepdims=True)).mean(axis=0)
    score = gain / (regret + REGRET_FLOOR) ** 2
    chance = score / score.sum() if score.sum() > 0 else np.full(len(allowed), 1 / len(allowed))
    index = rng.choice(len(allowed), p=chance)
    spread = np.sqrt(np.maximum(np.einsum('ij,jk,ik->i', contrast, cov, contrast), 0))
    return {'choice': allowed[index], 'cost': float(regret[index]), 'allowed': len(allowed), 'combinations': len(configs),
            'accuracy': float(1.645 * spread.mean()) if len(spread) else None,
            'least_certain': allowed[int(np.argmax(spread))] if len(spread) else None, 'qol': qol, 'sigma': sigma}


REGRET_DRAWS = 300  # Posterior draws for each candidate's expected cost.
REGRET_FLOOR = 0.3  # W: keeps a candidate that is surely best from taking every block.


def block_sigma(blocks):
    """Block-to-block noise after the workload correction (W), from the blocks so far."""
    if len(blocks) < len(COVARIATES) + 10:
        return None
    x = np.array([[1.0, *(b[c] for c in COVARIATES)] for b in blocks])
    y = np.array([b['bat'] for b in blocks])
    resid = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    return float(np.sqrt(np.sum(resid ** 2) / (len(y) - x.shape[1])))


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


def recommendations(levers, quality, energy=None, power=None):
    """The best measured combination for each goal, among those unlikely to earn a
    👎: 'battery' the lowest draw, 'comfort' the likeliest 👍 (then the lowest
    draw; Battery's until a 👍 exists to learn from). Each: the combination,
    `apply` (power-experiment apply arguments for all of it), what it changes
    from your settings (`change`, with `gain` and `interval` per full charge, and for comfort
    `delight`, the 👍 chance per block now → then) and all of it as `text`."""
    measured = {e['experiment'] for e in levers['effects']}
    fine = lambda c: qol_risk(quality, c) <= QOL_RISK
    learned = bool(quality['up']['votes'])
    keys = {'battery': None,
            'comfort': (lambda c: (-delight(quality, c), predicted(levers['coef'], c))) if learned else None}
    out = {}
    for goal, key in keys.items():
        best = best_config(levers, measured, energy, power, feasible=fine, key=key)
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
                now, then = (delight(quality, c) for c in (levers['current'], {**levers['current'], **best['config']}))
                entry['delight'] = f'{100 * now:.0f}% → {100 * then:.0f}%'
        entry['text'] = ' · '.join(filter(None, [entry['change'], entry['delight'] and f"👍 {entry['delight']}",
                                                 best and best['changes'] and gain_text(best)]))
        out[goal] = entry
    return out


def hours_needed(sigma, precision=0.5, block_minutes=BLOCK_MINUTES):
    """Battery hours until each setting's 90% interval is ±`precision` W (balanced blocks)."""
    blocks = (2 * 1.645 * sigma / precision) ** 2
    return blocks * block_minutes / 60


# --- Sleep ----------------------------------------------------------------------

def sleep_drain(events):
    """Suspends (gap events) with battery drain: start, hours, W, %/h."""
    result = []
    for event in events:
        if event.get('event') != 'gap':
            continue
        before, after, gap = event.get('bat_before') or {}, event.get('bat_after') or {}, event.get('gap_s', 0)
        hours = gap / 3600
        if hours < 0.25 or before.get('charge') is None or after.get('charge') is None:
            continue
        drop = before['charge'] - after['charge']
        if drop < 0:
            continue  # Charged while asleep.
        volts = before.get('volts') or after.get('volts') or 15.4
        wh = drop / 1e6 if before.get('unit') == 'uWh' else drop / 1e6 * volts
        result.append({'start': event['t1'] - gap, 'end': event['t1'], 'hours': hours, 'watts': wh / hours,
                       'pct_per_hour': ((before.get('pct') or 0) - (after.get('pct') or 0)) / hours,
                       'pct_before': before.get('pct'), 'pct_after': after.get('pct')})
    return result


def sleep_model(sleeps):
    """Log-normal fit of per-suspend drain: (log mean, predictive σ, suspends) or None.

    Drain is always positive and a few bad nights skew high; longer suspends
    weigh more (short ones are mostly counter rounding). σ is predictive for one
    new suspend: the spread between suspends plus the uncertainty of their mean.
    """
    rows = [(s['watts'], s['hours']) for s in sleeps if s['watts'] > 0.05 and s['hours'] >= 0.25]
    if len(rows) < 3:
        return None
    logs = np.log([w for w, _ in rows])
    weights = np.sqrt([h for _, h in rows])
    mean = float(np.average(logs, weights=weights))
    var = float(np.average((logs - mean) ** 2, weights=weights)) * len(rows) / (len(rows) - 1)
    effective = weights.sum() ** 2 / (weights ** 2).sum()
    return mean, math.sqrt(var * (1 + 1 / effective)), len(rows)


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
    """Per-minute stacks: [{'t', 'st', 'groups', 'apps', 'devices', 'bat', 'pct'}]."""
    out = []
    for m in minutes:
        parts = attribute(m, model)
        out.append({'t': m['t'], 'st': m['st'], 'groups': dict(parts['groups']), 'apps': dict(parts['apps']),
                    'devices': parts['devices'], 'bat': m.get('bat'), 'soc': m.get('soc'),
                    'pct': m.get('pct'), 'bl': m.get('bl'), 'exp': m.get('exp')})
    return out
