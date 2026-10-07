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
import collections
import datetime
import json
import math
import re
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

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
