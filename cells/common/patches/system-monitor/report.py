"""Battery history analysis for the battery panel.

Raw 10 s power-log records are folded into per-minute rows (cached per finished
day), then:

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
import os
import re
import time
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

from power import PowerLog

STATE_HOME = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
LOG = STATE_HOME / 'waybar-monitor/power'
CACHE = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'battery-panel'
CACHE_VERSION = 3
NUMERIC = ('bat', 'soc', 'load', 'busy', 'gpu', 'video', 'bl', 'kbd', 'wifi', 'disk', 'usb', 'audio')
STATUS = {'Discharging': 'D', 'Charging': 'C', 'Not charging': 'F', 'Full': 'F'}

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
PRIOR = {'floor': 6.0, 'load': 0.2, 'gpu': 3.0, 'video': 1.0,
         'base': 0.5, 'chip': 1.3, 'bl': 3.0, 'wifi': 0.3, 'disk': 2.0, 'usb': 0.3, 'audio': 0.5, 'kbd': 0.3}


def display_name(app):
    """Undo /proc/PID/comm truncation artifacts like 'openroad-wrapp'."""
    return re.sub(r'-w(?:r(?:a(?:p(?:p(?:e(?:d)?)?)?)?)?)?$|\.bin$', '', app)


def psr_enabled(mask):
    """PSR on/off from amdgpu.dcdebugmask. Records before it was logged ran with
    nixos-hardware's 0x10 on every boot, so a missing value means off."""
    try:
        return not int(mask, 0) & 0x10
    except (TypeError, ValueError):
        return False


def group_of(app):
    for group, prefixes in GROUPS:
        if app.startswith(prefixes):
            return group
    return 'Other apps'


# --- Per-minute aggregation ------------------------------------------------------

class Minute:
    def __init__(self, t):
        self.t, self.dt, self.sums, self.weights = t, 0.0, collections.defaultdict(float), collections.defaultdict(float)
        self.status, self.apps, self.last, self.exp, self.settings = collections.Counter(), {}, {}, None, {}
        self.boot = None

    def add(self, record):
        dt = record['dt']
        if dt <= 0:
            return
        self.dt += dt
        self.status[STATUS.get(record.get('status'), 'U')] += dt
        cpu, mhz = record.get('cpu', {}), record.get('cpu', {}).get('mhz')
        ghz2 = (mhz / 1000) ** 2 if mhz else None
        display = record.get('display', {})
        values = {
            'bat': record.get('bat', {}).get('w'), 'soc': record.get('soc', {}).get('w'),
            'busy': cpu.get('busy_s', 0) / dt, 'load': cpu.get('busy_s', 0) / dt * ghz2 if ghz2 else None,
            'gpu': record.get('gpu', {}).get('gpu', 0) / dt, 'video': record.get('gpu', {}).get('video', 0) / dt,
            'bl': display.get('bl'), 'kbd': display.get('kbd'),
            'wifi': sum(n.get('rx', 0) + n.get('tx', 0) for k, n in record.get('net', {}).items()
                        if k.startswith('wl')) / dt / 1e6,
            'disk': min(1.0, sum(d.get('busy', 0) for d in record.get('disk', {}).values()) / dt),
            'usb': len(record.get('usb', [])), 'audio': min(1, sum(record.get('audio', {}).values())),
        }
        for key, value in values.items():
            if value is not None:
                self.sums[key] += value * dt
                self.weights[key] += dt
        for name, app in record.get('apps', {}).items():
            if not (app.get('cpu') or app.get('gpu') or app.get('video')):
                continue
            entry = self.apps.setdefault(name, [0.0, 0.0, 0.0])
            entry[0] += app.get('cpu', 0) * (ghz2 or 0)
            entry[1] += app.get('gpu', 0)
            entry[2] += app.get('video', 0)
        battery = record.get('bat', {})
        for key in ('pct', 'charge', 'volts', 'unit'):
            if battery.get(key) is not None:
                self.last[key] = battery[key]
        settings = record.get('settings', {})
        self.boot = record.get('boot')
        self.settings = {'profile': settings.get('platform_profile'), 'aspm': settings.get('aspm'),
                         'psr': psr_enabled(settings.get('dcdebugmask')),
                         'boost': settings.get('boost'), 'abm': display.get('abm'),
                         'wifi_ps': ','.join(sorted((settings.get('wifi_ps') or {}).values())) or None,
                         'hz': next((o.get('hz') for o in display.get('niri', [])
                                     if str(o.get('name', '')).startswith('eDP')), None)}
        if (exp := record.get('exp')) is not None:
            washout = exp.get('washout', True) or (self.exp is not None and self.exp[4])
            if self.exp is not None and self.exp[:3] != [exp['run'], exp['name'], exp['block']]:
                washout = True  # Two blocks inside one minute.
            self.exp = [exp['run'], exp['name'], exp['block'], exp['arm'], washout, exp.get('value')]

    def row(self):
        row = {'t': self.t, 'dt': round(self.dt, 2), 'st': self.status.most_common(1)[0][0]}
        for key in NUMERIC:
            if self.weights.get(key):
                row[key] = round(self.sums[key] / self.weights[key], 4)
        if self.apps:
            row['apps'] = {name: [round(v / self.dt, 4) for v in values] for name, values in self.apps.items()}
        row.update({key: value for key, value in self.last.items()})
        row['set'] = self.settings
        if self.boot:
            row['boot'] = self.boot
        if self.exp is not None:
            row['exp'] = self.exp
        return row


def aggregate(records):
    """Fold records into per-minute rows plus the log's events, both sorted by time."""
    minutes, events = {}, []
    for record in records:
        if 'event' in record:
            events.append(record)
            continue
        if not isinstance(record.get('dt'), (int, float)) or 't1' not in record:
            continue
        t = int(record['t1'] // 60) * 60
        minutes.setdefault(t, Minute(t)).add(record)
    return [minutes[t].row() for t in sorted(minutes)], sorted(events, key=lambda e: e.get('t1', 0))


def read_records(path):
    try:
        with PowerLog.open(path) as source:
            for line in source:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError, ValueError):
        return


def read_from(path, offset):
    """Records of a plain log after `offset`, each with the byte offset of its line."""
    try:
        with open(path, 'rb') as source:
            source.seek(offset)
            position = offset
            for line in source:
                start, position = position, position + len(line)
                if not line.endswith(b'\n'):
                    return  # Being written.
                try:
                    yield start, json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def load_day(day, log=LOG, cache=CACHE):
    """Per-minute rows and events for one day, cached against the source file.

    Finished (compressed) days are cached whole. Today's growing file is cached
    up to its last complete minute with a byte offset, so a refresh only parses
    the new lines.
    """
    sources = [p for p in (log / f'{day}.jsonl', log / f'{day}.jsonl.zst', log / f'{day}.jsonl.gz') if p.exists()]
    if not sources:
        return [], []
    source = sources[0]
    stamp = [source.name, source.stat().st_size, int(source.stat().st_mtime)]
    cached = cache / f'{day}.json'
    try:
        data = json.loads(cached.read_text())
        if data.get('version') != CACHE_VERSION or data.get('source', [None])[0] != source.name:
            data = None
    except (OSError, ValueError, AttributeError):
        data = None
    if data and data['source'] == stamp and 'offset' not in data:
        return data['minutes'], data['events']
    if source.suffix == '.jsonl':
        resume = data if data and data.get('offset', 0) <= stamp[1] else {'minutes': [], 'events': [], 'offset': 0}
        lines = list(read_from(source, resume['offset']))
        fresh, events = aggregate(record for _, record in lines)
        minutes, events = resume['minutes'] + fresh, resume['events'] + events
        # Cache all but the newest minute, which may still be filling.
        offset = resume['offset']
        if fresh:
            newest = fresh[-1]['t']
            offset = next((start for start, record in lines if 'event' not in record and 't1' in record
                           and int(record['t1'] // 60) * 60 == newest), offset)
            complete = minutes[:-1]
            kept = [e for e in events if e.get('t1', 0) < newest]
        else:
            complete, kept = minutes, events
        payload = {'version': CACHE_VERSION, 'source': stamp, 'offset': offset, 'minutes': complete, 'events': kept}
    else:
        minutes, events = aggregate(read_records(source))
        payload = {'version': CACHE_VERSION, 'source': stamp, 'minutes': minutes, 'events': events}
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload))
    temporary.replace(cached)
    return minutes, events


def days_between(start, end):
    day = datetime.date.fromtimestamp(start)
    while time.mktime(day.timetuple()) < end:
        yield day.isoformat()
        day += datetime.timedelta(days=1)


def load_range(start, end, log=LOG, cache=CACHE):
    minutes, events = [], []
    days = available_days(log)
    if not days:
        return minutes, events
    start = max(start, time.mktime(time.strptime(days[0], '%Y-%m-%d')))
    for day in days_between(start, end):
        m, e = load_day(day, log, cache)
        minutes += [row for row in m if start <= row['t'] < end]
        events += [ev for ev in e if start <= ev.get('t1', 0) < end]
    return minutes, events


def available_days(log=LOG):
    return sorted({p.name.split('.')[0] for p in log.glob('*.jsonl*') if p.name[:4].isdigit()})


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


def what_if(rows, energy, replicates=200, block=10, seed=0):
    """Point estimate and 90% interval of runtime gain (minutes) per intervention.

    Moving-block bootstrap (10-minute blocks; residuals stay correlated for
    several minutes) refits both models on each resample, so intervals cover
    coefficient uncertainty as well as the period's variability.
    """
    if len(rows) < 30 or not energy:
        return [], None
    chip_x, chip_y, battery_x, battery_y = design(rows)
    model = fit(chip_x, chip_y, battery_x, battery_y)
    items = candidates(rows, model)
    dt = np.array([m['dt'] for m in rows])

    def evaluate(index, coef):
        power = float(np.average(battery_y[index], weights=dt[index]))
        return power, {name: runtime_gain(energy, power, float(np.average(vector(coef)[index], weights=dt[index])))
                       for name, vector in items.items()}
    everything = np.arange(len(rows))
    power, point = evaluate(everything, model)
    saved = {name: float(np.average(vector(model), weights=dt)) for name, vector in items.items()}
    rng = np.random.default_rng(seed)
    samples = collections.defaultdict(list)
    for _ in range(replicates):
        index = block_indices(len(rows), block, rng)
        coef = fit(chip_x[index], chip_y[index], battery_x[index], battery_y[index])
        for name, gain in evaluate(index, coef)[1].items():
            samples[name].append(gain)
    estimates = []
    for name, gain in point.items():
        low, high = np.percentile(samples[name], [5, 95])
        estimates.append({'name': name, 'evidence': 'model', 'watts': saved[name], 'minutes': gain,
                          'low': float(low), 'high': float(high)})
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


def sleep_runtime(sleeps, energy_wh, z=1.2816):
    """Hours a suspend would last from `energy_wh` (median, low, high of an 80% interval).

    Per-suspend drain rates are fitted as log-normal (always positive, a few bad
    nights skew high), weighting longer suspends more (short ones are mostly
    counter rounding). The interval is predictive for one new suspend: the
    spread between suspends plus the uncertainty of their mean.
    """
    rows = [(s['watts'], s['hours']) for s in sleeps if s['watts'] > 0.05 and s['hours'] >= 0.25]
    if len(rows) < 3 or not energy_wh:
        return None
    logs = np.log([w for w, _ in rows])
    weights = np.sqrt([h for _, h in rows])
    mean = float(np.average(logs, weights=weights))
    var = float(np.average((logs - mean) ** 2, weights=weights)) * len(rows) / (len(rows) - 1)
    effective = weights.sum() ** 2 / (weights ** 2).sum()
    sigma = math.sqrt(var * (1 + 1 / effective))
    watts = math.exp(mean)
    return {'hours': energy_wh / watts, 'low': energy_wh / math.exp(mean + z * sigma),
            'high': energy_wh / math.exp(mean - z * sigma), 'watts': watts, 'suspends': len(rows)}


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
