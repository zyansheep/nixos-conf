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
import re
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, nnls
from scipy.special import expit

import power
import store

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
    starts = rng.integers(0, max(1, n - block + 1), size=math.ceil(n / block))
    return np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]


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
    """Paired consecutive A/B blocks per run → saving of arm B (W) and runtime gain."""
    blocks = experiment_blocks(minutes)
    pairs = collections.defaultdict(list)
    arms = collections.defaultdict(list)
    for block in blocks.values():
        arms[block['name']].append(block)
    for run in sorted({key[0] for key in blocks}):
        sequence = sorted((key[1], b) for key, b in blocks.items() if key[0] == run)
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

    def estimate(groups):
        sample = [(m, arm) for arm in (False, True) for boot in groups[arm] for m in boot]
        x = np.array([[1.0, m['load'], m.get('gpu', 0), m.get('video', 0), m.get('bl') or 0,
                       m.get('wifi', 0), m.get('disk', 0), float(arm)] for m, arm in sample])
        y = np.array([m['bat'] for m, _ in sample])
        coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        return -float(coef[-1])  # Watts saved with the setting on.
    groups = {arm: list(boots.values()) for arm, boots in arms.items()}
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
           'boost': 'boost', 'profile': 'profile'}


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
    except (TypeError, ValueError):
        return False
    return False


# These change CPU frequency, and so the CPU load the workload correction
# uses. A consequence of the setting must not be adjusted away, so they are
# estimated without that correction and need more battery time.
CPU_LEVERS = {'boost', 'profile'}
COVARIATES = ('load', 'gpu', 'video', 'bl', 'kbd', 'wifi', 'disk', 'usb', 'audio')
PRIOR_SD = {'main': 5.0, 'pair': 1.0}  # W: interactions are shrunk (mildly) toward zero.
BLOCK_MINUTES = 4  # power-experiment's blocks (experiment.BLOCK); the first minute is washout.


def lever_states(settings, top_hz, chosen=None):
    """{name: in its saving state}. `chosen` — the values an experiment set for the
    settings it randomized — wins over what was logged: logging can lag, and
    before 2026-10-07 the APST value logged was nvme_core's boot default."""
    chosen = chosen or {}
    return {name: saving(name, chosen[name] if name in chosen else settings.get(key), top_hz)
            for name, key in SETTING.items()}


def experiment_values(exp, levers):
    """The values a minute's experiment set: a dict for several settings, a value for one."""
    return exp[5] if isinstance(exp[5], dict) else {levers[0]: exp[5]} if len(levers) == 1 else {}


def top_refresh(minutes):
    return max((m['set'].get('hz') for m in minutes if m.get('set', {}).get('hz')), default=None)


def lever_blocks(minutes):
    """Clean blocks of setting experiments (single or combined): battery W and
    workload means, and which settings were in their saving state."""
    top = top_refresh(minutes)
    acc = {}
    for m in minutes:
        exp = m.get('exp')
        if not exp or exp[4] or m['st'] != 'D' or m.get('bat') is None or m.get('load') is None or m['dt'] < 40:
            continue
        levers = [name for name in str(exp[1]).split('+') if name in SETTING]
        if not levers:
            continue  # App-freezing runs are analysed as paired A/B blocks.
        chosen = experiment_values(exp, levers)
        # Baselines are per run and day: an automatic run goes on for weeks.
        group = f"{exp[0]}@{datetime.date.fromtimestamp(m['t']).isoformat()}"
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
    sw = np.sqrt(w)
    a = np.vstack([x * sw[:, None], np.diag(np.sqrt(penalty))])
    return np.linalg.lstsq(a, np.concatenate([y * sw, np.zeros(len(penalty))]), rcond=None)[0]


def lever_effects(minutes, energy, power, replicates=300, block=3, seed=0, min_blocks=3):
    """Savings of each experimented setting, and of pairs of them together.

    One regression over clean blocks of every setting experiment: battery W on
    a baseline per run (so a setting is only ever compared within the run that
    randomized it), workload, an indicator per setting (saving state) and per
    pair of settings randomized together (shrunk toward zero: most pairs don't
    interact). CPU settings come from a second fit without the CPU-load
    correction. Effects are for the settings as they were in the latest
    battery minute. Intervals: moving-block bootstrap over consecutive blocks.
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
    battery = [m for m in minutes if m['st'] == 'D' and m.get('set')] or [m for m in minutes if m.get('set')]
    current = lever_states(battery[-1]['set'] if battery else {}, top_refresh(minutes))
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
        coef = {}
        for cpu in (False, True):
            base = len(runs) + len(covariates[cpu])
            penalty = np.array([1e-6] * base + [sigma2[cpu] / PRIOR_SD['main'] ** 2] * len(varying)
                               + [sigma2[cpu] / PRIOR_SD['pair'] ** 2] * len(pairs))
            beta = ridge(x[cpu][index], y[index], w[index], penalty)
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
    draw = lambda i: {key: values[i] for key, values in samples.items()}
    result['effects'] = [dict(summary(lever(coef, n), [lever(draw(i), n) for i in range(replicates)]),
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


def best_config(result, allowed, energy=None, power=None, feasible=None):
    """The measured combination of `allowed` settings with the lowest predicted
    draw (others stay as they are) among those `feasible` accepts, and its
    saving against the current one."""
    names = [e['experiment'] for e in result['effects'] if e['experiment'] in allowed]
    if not names:
        return None
    current = dict(result['current'])
    configs = [dict(current, **dict(zip(names, combo))) for combo in itertools.product((False, True), repeat=len(names))]
    configs = [c for c in configs if feasible is None or feasible(c) or c == current]
    best = min(configs, key=lambda c: predicted(result['coef'], c))
    saving = predicted(result['coef'], current) - predicted(result['coef'], best)
    draws = [predicted({k: v[i] for k, v in result['samples'].items()}, current)
             - predicted({k: v[i] for k, v in result['samples'].items()}, best)
             for i in range(len(next(iter(result['samples'].values()))))]
    low, high = np.percentile(draws, [5, 95])
    out = {'config': {n: best[n] for n in names}, 'changes': [n for n in names if best[n] != current.get(n)],
           'watts': saving, 'watts_low': float(low), 'watts_high': float(high)}
    if energy and power:
        out.update(minutes=runtime_gain(energy, power, saving), low=runtime_gain(energy, power, float(low)),
                   high=runtime_gain(energy, power, float(high)))
    return out


# --- Quality of life --------------------------------------------------------------------

QOL_RISK = 0.1  # Acceptable chance per 4-minute block that a combination earns a 👎.
# Settings you might notice (experiment.catalog()'s `visible`), and Beta priors on
# the chance per block that each earns a 👎, most likely 1% quiet,
# 4% noticeable, 2% for reasons that have nothing to do with the settings — worth
# 10-20 blocks of evidence, so a few complaints outweigh them.
NOTICEABLE = {'abm', 'refresh', 'boost', 'profile', 'wifi-ps'}
QOL_PRIOR = {'quiet': (1.2, 20.8), 'noticeable': (1.4, 10.6), 'base': (1.4, 20.6)}


def qol_drops(events):
    """Times of 👎 votes."""
    return sorted(e['t1'] for e in events if e.get('event') == 'qol' and e.get('vote') == 'down')


def qol_windows(minutes, events):
    """(setting states, whether you gave a 👎) per 4-minute window — an
    experiment block, or a stretch outside experiments — that you were there for."""
    drops, top = qol_drops(events), top_refresh(minutes)
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
        dropped = bisect.bisect_left(drops, w['t0']) < bisect.bisect_left(drops, w['t1'])
        out.append((dict(w['states'].most_common(1)[0][0]), dropped))
    return out


def qol_model(minutes, events, noticeable=NOTICEABLE):
    """Noisy-OR: each setting in its saving state has its own chance per block of
    earning a 👎, on top of a base chance; fitted by maximum a posteriori over
    the windows, so it starts from the priors."""
    windows = qol_windows(minutes, events)
    names = list(SETTING)
    priors = np.array([QOL_PRIOR['base']] + [QOL_PRIOR['noticeable' if n in noticeable else 'quiet'] for n in names])
    x = np.array([[1.0] + [float(states[n]) for n in names] for states, _ in windows]).reshape(-1, 1 + len(names))
    y = np.array([float(dropped) for _, dropped in windows])

    def loss(theta):
        p = np.clip(expit(theta), 1e-9, 1 - 1e-9)
        fine = x @ np.log1p(-p)  # log P(fine) per window
        ll = np.sum((1 - y) * fine + y * np.log(-np.expm1(np.minimum(fine, -1e-12))))
        return -(ll + np.sum((priors[:, 0] - 1) * np.log(p) + (priors[:, 1] - 1) * np.log1p(-p)))
    mode = (priors[:, 0] - 1) / (priors.sum(axis=1) - 2)  # Without windows the fit is the prior's mode.
    p = expit(minimize(loss, np.log(mode / (1 - mode)), method='L-BFGS-B').x)
    count = lambda n, flag: sum(1 for states, dropped in windows if states[n] and (dropped or not flag))
    return {'base': float(p[0]), 'p': {n: float(p[1 + i]) for i, n in enumerate(names)}, 'windows': len(windows),
            'drops': int(y.sum()), 'tally': {n: {'drops': count(n, True), 'windows': count(n, False)} for n in names}}


def qol_risk(model, config):
    """Chance per block that `config` ({name: saving state}) earns a 👎."""
    fine = (1 - model['base']) * math.prod(1 - model['p'][n] for n, on in config.items() if on and n in model['p'])
    return 1 - fine


# --- Automatic experiments: where to look next ------------------------------------------------

def features(config, names):
    pairs = itertools.combinations(names, 2)
    return [float(config[n]) for n in names] + [float(config[a] and config[b]) for a, b in pairs]


def plan(minutes, events, names, present=True, sigma=None, rng=None, noticeable=NOTICEABLE):
    """The next block's combination of `names` ({name: saving state}) for automatic experiments.

    The setting model's ridge fit is a Bayesian linear model (its penalties are
    the Gaussian prior), so its posterior is closed-form. Candidates are the
    combinations whose quality-of-life risk is acceptable (all of them while
    nobody is there). For each, the expected reduction in the variance of the
    model's predictions across all candidates if it were observed next —
    Σ_c Cov(c, x)² / (σ² + Var x), a greedy integrated-variance design — and
    one is sampled in proportion to it. Today's baseline is part of the
    posterior, so a block that would only pin it down earns nothing.
    """
    rng = rng or np.random.default_rng()
    blocks = lever_blocks(minutes)
    sigma = sigma or block_sigma(blocks) or 2.0
    groups = sorted({b['run'] for b in blocks} | {'next'})
    k = len(groups) + len(COVARIATES)
    rows = np.array([[*(float(b['run'] == g) for g in groups), *(b[c] for c in COVARIATES), *features(b['x'], names)]
                     for b in blocks]).reshape(-1, k + len(features(dict.fromkeys(names, False), names)))
    weights = np.array([b['seconds'] / 180 for b in blocks])  # A clean block is 3 minutes.
    prior_sd = ([100.0] * len(groups) + [10.0] * len(COVARIATES) + [PRIOR_SD['main']] * len(names)
                + [PRIOR_SD['pair']] * (len(names) * (len(names) - 1) // 2))
    precision = rows.T @ (rows * weights[:, None]) / sigma ** 2 + np.diag(1 / np.array(prior_sd) ** 2)
    cov = np.linalg.inv(precision)
    qol = qol_model(minutes, events, noticeable)
    configs = [dict(zip(names, combo)) for combo in itertools.product((False, True), repeat=len(names))]
    allowed = [c for c in configs if not present or qol_risk(qol, c) <= QOL_RISK] or [dict.fromkeys(names, False)]
    recent = [m for m in minutes[-30:] if m['st'] == 'D'] or minutes[-30:]
    workload = [float(np.mean([m.get(c) or 0 for m in recent])) if recent else 0.0 for c in COVARIATES]
    # A prediction of interest is a contrast (no baseline); an observation includes today's.
    contrast = np.array([[0.0] * k + features(c, names) for c in allowed])
    observed = np.array([[float(g == 'next') for g in groups] + workload + features(c, names) for c in allowed])
    cross = contrast @ cov @ observed.T
    gain = (cross ** 2).sum(axis=0) / (sigma ** 2 + np.einsum('ij,jk,ik->i', observed, cov, observed))
    chance = gain / gain.sum() if gain.sum() > 0 else np.full(len(allowed), 1 / len(allowed))
    choice = allowed[rng.choice(len(allowed), p=chance)]
    spread = np.sqrt(np.maximum(np.einsum('ij,jk,ik->i', contrast, cov, contrast), 0))
    return {'choice': choice, 'allowed': len(allowed), 'combinations': len(configs),
            'accuracy': float(1.645 * spread.mean()) if len(spread) else None,
            'least_certain': allowed[int(np.argmax(spread))] if len(spread) else None, 'qol': qol, 'sigma': sigma}


def block_sigma(blocks):
    """Block-to-block noise after the workload correction (W), from the blocks so far."""
    if len(blocks) < len(COVARIATES) + 10:
        return None
    x = np.array([[1.0, *(b[c] for c in COVARIATES)] for b in blocks])
    y = np.array([b['bat'] for b in blocks])
    resid = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    return float(np.sqrt(np.sum(resid ** 2) / (len(y) - x.shape[1])))


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
