"""Battery attribution for the power menu and a long-term raw power log.

Every RECORD seconds one JSON line of raw measurements (battery and chip power,
per-app CPU/GPU/IO, devices and power settings) is appended to
`<state>/power/YYYY-MM-DD.jsonl`; earlier days are zstd-compressed. The menu
splits measured power with two small online ridge models. The log keeps the
model inputs rather than its outputs, so better models can be fitted later.
App names are coarse (no PIDs, command lines, browser origins or titles).
"""
import collections
import datetime
import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

try:
    from compression import zstd
except ImportError:  # Python < 3.14
    zstd = None

SCHEMA = 1
RECORD = 10        # seconds per log record
WINDOW = 30 * 60   # seconds of history behind the menu
TICKS = os.sysconf('SC_CLK_TCK')


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return default


def number(path, default=None):
    try:
        return float(read(path))
    except (TypeError, ValueError):
        return default


def fraction(directory):
    value, top = number(directory / 'brightness'), number(directory / 'max_brightness')
    return round(value / top, 4) if value is not None and top else None


# --- Sub-second power readings -------------------------------------------------

def find_battery(root):
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for path in entries:
        if (read(path / 'type') == 'Battery' and read(path / 'scope') != 'Device'
                and number(path / 'present', 1) != 0):
            return path
    return None


def mains_online(root):
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    mains = [number(path / 'online') for path in entries if read(path / 'type') == 'Mains']
    return any(value == 1 for value in mains) if mains else None


def battery_watts(path):
    """Signed battery power: positive while discharging, negative while charging."""
    status = read(path / 'status', 'Unknown')
    power = number(path / 'power_now')
    if power is None:
        current, voltage = number(path / 'current_now'), number(path / 'voltage_now')
        if current is None or voltage is None:
            return status, None
        power = current * voltage / 1e6  # µA × µV → µW
    watts = abs(power) / 1e6
    return status, watts if status == 'Discharging' else -watts if status == 'Charging' else 0.0


def find_hwmon(root, name):
    for directory in sorted(root.glob('hwmon*')):
        if read(directory / 'name') == name:
            return directory
    return None


class FastSampler:
    """Battery and chip power several times per record; the EC updates about 1 Hz."""

    def __init__(self, sys=Path('/sys')):
        self.supplies = sys / 'class/power_supply'
        self.battery = find_battery(self.supplies)
        amdgpu = find_hwmon(sys / 'class/hwmon', 'amdgpu')
        self.chip = next((amdgpu / name for name in ('power1_input', 'power1_average')
                          if amdgpu and (amdgpu / name).exists()), None)
        self.gpu_busy = next(iter(sorted(sys.glob('class/drm/card*/device/gpu_busy_percent'))), None)
        self.reset()

    def reset(self):
        self.values = collections.defaultdict(list)
        self.status = None

    def sample(self):
        if self.battery is None:
            self.battery = find_battery(self.supplies)
        if self.battery is not None:
            self.status, watts = battery_watts(self.battery)
            if watts is not None:
                self.values['bat'].append(watts)
        if self.chip is not None and (value := number(self.chip)) is not None:
            self.values['soc'].append(value / 1e6)
        if self.gpu_busy is not None and (value := number(self.gpu_busy)) is not None:
            self.values['gpu_busy'].append(value)

    def take(self):
        result = {name: {'w': round(sum(v) / len(v), 3), 'min': round(min(v), 3),
                         'max': round(max(v), 3), 'n': len(v)}
                  for name, v in self.values.items() if v}
        status = self.status
        self.reset()
        return result, status


# --- Cumulative counters read once per record ----------------------------------

def cpu_times(proc=Path('/proc')):
    result = {}
    try:
        with open(proc / 'stat') as source:
            for line in source:
                parts = line.split()
                if parts[0] == 'cpu':
                    user, nice, system, idle, iowait, irq, softirq, steal = map(int, parts[1:9])
                    result.update(user=(user + nice) / TICKS, sys=system / TICKS,
                                  irq=(irq + softirq) / TICKS, iowait=iowait / TICKS,
                                  idle=idle / TICKS, steal=steal / TICKS)
                elif parts[0] in ('ctxt', 'intr'):
                    result[parts[0]] = int(parts[1])
    except (OSError, ValueError, IndexError):
        pass
    return result


class IdleStates:
    """Summed C-state residency per state name across CPUs."""

    def __init__(self, sys=Path('/sys')):
        self.paths = [(read(state / 'name', state.name), state / 'time')
                      for state in sorted(sys.glob('devices/system/cpu/cpu[0-9]*/cpuidle/state[0-9]*'))]

    def read(self):
        totals = collections.defaultdict(float)
        for name, path in self.paths:
            if (value := number(path)) is not None:
                totals[name] += value / 1e6
        return dict(totals)


def cpu_frequency(sys=Path('/sys')):
    values = [number(path) for path in sys.glob('devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq')]
    values = [v / 1000 for v in values if v]
    return (sum(values) / len(values), max(values)) if values else (None, None)


def interrupts(proc=Path('/proc')):
    counts = {}
    try:
        with open(proc / 'interrupts') as source:
            cpus = len(source.readline().split())
            for line in source:
                label, _, rest = line.partition(':')
                parts = rest.split()
                values = []
                for part in parts[:cpus]:
                    if not part.isdigit():
                        break
                    values.append(int(part))
                label = label.strip()
                # Numbered lines: counts, chip, hwirq-type, then device names.
                name = ' '.join(parts[len(values) + 2:]) if label.isdigit() else ''
                counts[f'{label} {name}'.strip()[:48]] = sum(values)
    except OSError:
        pass
    return counts


def network(sys=Path('/sys')):
    result = {}
    for iface in sorted((sys / 'class/net').glob('*')):
        if iface.name == 'lo':
            continue
        stats = iface / 'statistics'
        result[iface.name] = {key: int(number(stats / name, 0)) for key, name in (
            ('rx', 'rx_bytes'), ('tx', 'tx_bytes'), ('rxp', 'rx_packets'), ('txp', 'tx_packets'))}
        result[iface.name]['state'] = read(iface / 'operstate', '')
    return result


def disks(sys=Path('/sys')):
    result = {}
    for device in sorted((sys / 'block').glob('*')):
        if not device.name.startswith(('nvme', 'sd', 'mmcblk')):
            continue
        try:
            fields = [int(v) for v in read(device / 'stat', '').split()]
            result[device.name] = {'rd': fields[2] * 512, 'wr': fields[6] * 512, 'busy': fields[9] / 1000}
        except (ValueError, IndexError):
            continue
    return result


def wifi_signal(proc=Path('/proc')):
    try:
        lines = (proc / 'net/wireless').read_text().splitlines()[2:]
        return {line.split(':')[0].strip(): float(line.split()[3].rstrip('.')) for line in lines}
    except (OSError, ValueError, IndexError):
        return {}


def usb_active(sys=Path('/sys')):
    active = []
    for device in sorted((sys / 'bus/usb/devices').glob('*')):
        if ':' in device.name or device.name.startswith('usb'):
            continue  # Interfaces and root hubs.
        if read(device / 'power/runtime_status') == 'active':
            active.append(read(device / 'product') or
                          f"{read(device / 'idVendor', '?')}:{read(device / 'idProduct', '?')}")
    return active


def pci_active(sys=Path('/sys')):
    counts = collections.Counter()
    for device in sorted((sys / 'bus/pci/devices').glob('*')):
        if read(device / 'power/runtime_status') == 'active':
            try:
                counts[(device / 'driver').readlink().name] += 1
            except OSError:
                counts[read(device / 'class', '?')] += 1
    return dict(counts)


def audio_streams(proc=Path('/proc')):
    result = {'play': 0, 'rec': 0}
    for status in proc.glob('asound/card*/pcm*/sub*/status'):
        if (read(status) or '').startswith('state: RUNNING'):
            result['play' if status.parent.parent.name.endswith('p') else 'rec'] += 1
    return result


def display(sys=Path('/sys')):
    result = {}
    backlight = next(iter(sorted((sys / 'class/backlight').glob('*'))), None)
    if backlight is not None:
        result['bl'] = fraction(backlight)
    keyboard = [f for f in (fraction(led) for led in (sys / 'class/leds').glob('*kbd_backlight*'))
                if f is not None]
    if keyboard:
        result['kbd'] = max(keyboard)
    outputs = []
    for connector in sorted((sys / 'class/drm').glob('card*-*')):
        if read(connector / 'status') != 'connected':
            continue
        name = connector.name.partition('-')[2]
        outputs.append({'name': name, 'enabled': read(connector / 'enabled'), 'dpms': read(connector / 'dpms')})
        if name.startswith(('eDP', 'LVDS', 'DSI')):
            abm = number(connector / 'amdgpu/panel_power_savings')
            if abm is not None:
                result['abm'] = int(abm)
    result['outputs'] = outputs
    return result


def settings(sys=Path('/sys')):
    cpu = sys / 'devices/system/cpu'
    values = {
        'platform_profile': read(sys / 'firmware/acpi/platform_profile'),
        'epp': read(cpu / 'cpu0/cpufreq/energy_performance_preference'),
        'governor': read(cpu / 'cpu0/cpufreq/scaling_governor'),
        'boost': read(cpu / 'cpufreq/boost'),
        'amd_pstate': read(cpu / 'amd_pstate/status'),
        'aspm': next((w[1:-1] for w in (read(sys / 'module/pcie_aspm/parameters/policy') or '').split()
                      if w.startswith('[')), None),
        'nvme_apst_us': read(sys / 'module/nvme_core/parameters/default_ps_max_latency_us'),
        # Boot-level: bit 0x10 disables panel self-refresh (see the psr specialisation).
        'dcdebugmask': read(sys / 'module/amdgpu/parameters/dcdebugmask'),
    }
    if (limit := number(cpu / 'cpu0/cpufreq/scaling_max_freq')) is not None:
        values['max_mhz'] = round(limit / 1000)
    battery = find_battery(sys / 'class/power_supply')
    if battery is not None:
        values['charge_limit'] = number(battery / 'charge_control_end_threshold')
    for switch in sorted((sys / 'class/rfkill').glob('rfkill*')):
        values[f"rfkill_{read(switch / 'type')}"] = 'on' if read(switch / 'soft') == read(switch / 'hard') == '0' else 'off'
    return {key: value for key, value in values.items() if value is not None}


def thermal(sys=Path('/sys')):
    result = {}
    if (k10temp := find_hwmon(sys / 'class/hwmon', 'k10temp')) and (value := number(k10temp / 'temp1_input')) is not None:
        result['temp'] = value / 1000
    for name in ('framework_laptop', 'cros_ec'):
        if (hwmon := find_hwmon(sys / 'class/hwmon', name)) and (value := number(hwmon / 'fan1_input')) is not None:
            result['fan'] = value
            break
    return result


def lid_state(proc=Path('/proc')):
    for path in proc.glob('acpi/button/lid/*/state'):
        words = (read(path) or '').split()
        return words[-1] if words else None
    return None


class Commands:
    """Slow-changing state that needs a helper process; refreshed once a minute."""

    def __init__(self, interval=60):
        self.interval, self.when, self.value = interval, None, {}

    def read(self, now):
        if self.when is not None and now - self.when < self.interval:
            return self.value
        self.when, self.value = now, {}
        if shutil.which('iw'):
            output = self.run(['iw', 'dev'])
            for iface in [line.split()[1] for line in output.splitlines() if line.strip().startswith('Interface ')]:
                state = self.run(['iw', 'dev', iface, 'get', 'power_save'])
                if 'Power save:' in state:
                    self.value.setdefault('wifi_ps', {})[iface] = state.split(':')[1].strip()
        if shutil.which('niri') and os.environ.get('NIRI_SOCKET'):
            try:
                outputs = json.loads(self.run(['niri', 'msg', '-j', 'outputs']) or '{}')
                self.value['niri'] = [niri_output(o) for o in outputs.values() if isinstance(o, dict)]
            except ValueError:
                pass
        return self.value

    @staticmethod
    def run(command):
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return ''


def niri_output(output):
    modes, current = output.get('modes') or [], output.get('current_mode')
    mode = modes[current] if isinstance(current, int) and 0 <= current < len(modes) else {}
    logical = output.get('logical') or {}
    return {'name': output.get('name'), 'w': mode.get('width'), 'h': mode.get('height'),
            'hz': mode.get('refresh_rate') and round(mode['refresh_rate'] / 1000, 2),
            'vrr': output.get('vrr_enabled'), 'scale': logical.get('scale'), 'on': bool(mode)}


# --- Per-process GPU and IO ----------------------------------------------------

VIDEO_ENGINES = ('dec', 'enc', 'jpeg', 'vcn', 'vpe', 'video')


def engine_group(engine):
    if engine in ('gfx', 'compute', 'render'):
        return 'gpu'
    return 'video' if engine.startswith(VIDEO_ENGINES) else engine


def parse_fdinfo(text):
    client, engines = None, {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        if key == 'drm-client-id':
            client = value.strip()
        elif key.startswith('drm-engine-') and not key.startswith('drm-engine-capacity'):
            try:
                engines[key[len('drm-engine-'):]] = int(value.split()[0])
            except (ValueError, IndexError):
                continue
    return client, engines


class GpuClients:
    """Per-app GPU engine time from DRM fdinfo, deduplicated by client id."""

    def __init__(self, proc=Path('/proc'), rescan=60):
        self.proc, self.rescan = proc, rescan
        self.fds, self.scanned, self.previous = {}, {}, None

    def scan(self, pid):
        found = []
        try:
            for entry in os.scandir(self.proc / str(pid) / 'fd'):
                try:
                    if os.readlink(entry.path).startswith('/dev/dri/'):
                        found.append(entry.name)
                except OSError:
                    continue
        except OSError:
            pass
        return found

    def sample(self, now, processes):
        current = {}
        for key in list(self.scanned):
            if key not in processes:
                del self.scanned[key], self.fds[key]
        for key, entry in processes.items():
            if key not in self.scanned or now - self.scanned[key] >= self.rescan:
                self.fds[key], self.scanned[key] = self.scan(key[0]), now
            for fd in self.fds[key]:
                client, engines = parse_fdinfo(read(self.proc / str(key[0]) / 'fdinfo' / fd, ''))
                if client is not None and engines:
                    current.setdefault(client, (entry[3], engines))
        usage = collections.defaultdict(lambda: collections.defaultdict(float))
        if self.previous is not None:
            for client, (app, engines) in current.items():
                before = self.previous.get(client)
                if before is None:
                    continue  # Opened since the last record; its history is unknown.
                for engine, ns in engines.items():
                    if (delta := ns - before[1].get(engine, ns)) > 0:
                        usage[app][engine_group(engine)] += delta / 1e9
        self.previous = current
        return usage


class Deltas:
    """Per-app increase of cumulative per-process counters, safe against PID reuse."""

    def __init__(self):
        self.previous, self.since = None, None

    def sample(self, boot, values, apps):
        usage = collections.defaultdict(lambda: collections.defaultdict(float))
        for key, counters in values.items():
            if key not in apps:
                continue  # Not in this scan (raced with process start/exit).
            before = self.previous.get(key) if self.previous is not None else None
            if before is None:
                # Count a process from zero only if it started after the last sample.
                if self.since is None or key[1] / TICKS < self.since:
                    continue
                before = {}
            for name, value in counters.items():
                if (delta := value - before.get(name, 0)) > 0:
                    usage[apps[key]][name] += delta
        self.previous, self.since = values, boot
        return usage


def process_io(proc, keys):
    result = {}
    for key in keys:
        try:
            counters = {}
            with open(proc / str(key[0]) / 'io') as source:
                for line in source:
                    name, _, value = line.partition(':')
                    if name in ('read_bytes', 'write_bytes'):
                        counters['io_r' if name == 'read_bytes' else 'io_w'] = int(value)
            result[key] = counters
        except (OSError, ValueError):
            continue
    return result


# --- Online models and attribution ---------------------------------------------

# Model terms and the priors they start from. battery-eta trains the real
# values (report.fit) into models.json every 15 minutes; the collector only
# reads them. report.py shares this dict.
PRIOR = {'floor': 6.0, 'load': 0.2, 'gpu': 3.0, 'video': 1.0,
         'base': 0.5, 'chip': 1.3, 'bl': 3.0, 'wifi': 0.3, 'disk': 2.0, 'usb': 0.3, 'audio': 0.5, 'kbd': 0.3}


def load_attribution(path):
    """Trained attribution parameters from battery-eta's models.json, or None."""
    try:
        data = json.loads(Path(path).read_text())
        return {**PRIOR, **{k: float(v) for k, v in data['attribution'].items() if k in PRIOR}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clock_squared(record):
    mhz = record.get('cpu', {}).get('mhz')
    return (mhz / 1000) ** 2 if mhz else None


def platform_features(record):
    dt, shown = record['dt'], record.get('display', {})
    wifi = sum(n.get('rx', 0) + n.get('tx', 0) for name, n in record.get('net', {}).items() if name.startswith('wl'))
    return [1.0, record.get('soc', {}).get('w', 0), shown.get('bl') or 0, wifi / dt / 1e6,
            min(1.0, sum(d.get('busy', 0) for d in record.get('disk', {}).values()) / dt),
            len(record.get('usb', [])), min(1, sum(record.get('audio', {}).values())), shown.get('kbd') or 0]


def discharging(record):
    return record.get('status') == 'Discharging' and 'w' in record.get('bat', {})


DEVICE_TERMS = (('Display', 'bl', 2), ('Wi-Fi', 'wifi', 3), ('Storage', 'disk', 4),
                ('USB devices', 'usb', 5), ('Audio', 'audio', 6), ('Keyboard backlight', 'kbd', 7))


def attribute(record, params):
    """Split one record's measured energy (joules) across apps and hardware.

    On battery each chip watt is scaled by the model's per-chip-watt cost (k),
    so apps carry their share of conversion losses; on AC only chip power is
    known. `params` are battery-eta's trained values (see PRIOR).
    """
    dt, rows = record['dt'], collections.defaultdict(float)
    soc = record.get('soc', {}).get('w')
    if soc is None:
        return rows
    on_battery = discharging(record)
    scale = params['chip'] if on_battery else 1.0
    floor = min(soc, params['floor'])
    ghz2 = clock_squared(record) or 0
    weights = {}
    apps = record.get('apps', {})
    for name, app in apps.items():
        weights[name] = (params['load'] * app.get('cpu', 0) * ghz2
                         + params['gpu'] * app.get('gpu', 0) + params['video'] * app.get('video', 0)) / dt
    unowned = record.get('cpu', {}).get('busy_s', 0) - sum(a.get('cpu', 0) for a in apps.values())
    weights['Kernel'] = weights.get('Kernel', 0) + params['load'] * max(0, unowned) * ghz2 / dt
    total = sum(weights.values())
    if total > 0:
        for name, weight in weights.items():
            if weight > 0:
                rows[name] += scale * (soc - floor) * dt * weight / total
    else:
        floor = soc
    rows['Processor baseline'] += scale * floor * dt
    if on_battery:
        rest = record['bat']['w'] - scale * soc
        x = platform_features(record)
        parts = {name: params[term] * x[index] for name, term, index in DEVICE_TERMS}
        modelled = sum(parts.values())
        shrink = min(1.0, max(0.0, rest) / modelled) if modelled > 0 else 0
        for name, watts in parts.items():
            if watts > 0:
                rows[name] += watts * shrink * dt
        rows['Rest of system'] += max(0.0, rest - modelled) * dt
    return rows


def summarize(window, now, log_totals, limit=6):
    """Menu block: average watts per row over the recent window."""
    battery = [w for w in window if w[2]]
    use = battery or window
    seconds = sum(w[1] for w in use)
    totals = collections.defaultdict(float)
    for _, _, _, rows in use:
        for name, joules in rows.items():
            totals[name] += joules
    energy = sum(totals.values())
    minutes = max(1, round(seconds / 60))
    if not use or seconds <= 0:
        title = 'Battery use: measuring…'
    elif battery:
        title = f'Battery use · last {minutes} min on battery · {energy / seconds:.1f} W'
    else:
        title = f'On AC · chip power by app, last {minutes} min · {energy / seconds:.1f} W'
    rows = [{'name': name, 'value': f'{joules / seconds:4.1f} W {100 * joules / energy:3.0f}%'}
            for name, joules in sorted(totals.items(), key=lambda r: (-r[1], r[0]))[:limit]
            if seconds > 0 and energy > 0 and joules / seconds >= 0.05]
    return {'title': title, 'rows': rows, 'footer': log_footer(*log_totals), 'timestamp': now}


def log_footer(records, seconds, first):
    if not records:
        return 'Power log: no samples yet'
    span = f'{seconds / 3600:.1f} h' if seconds >= 3600 else f'{seconds / 60:.0f} min'
    since = datetime.datetime.fromtimestamp(first).strftime('%b %-d') if first else '?'
    return f'Power log: {records:,} samples · {span} since {since}'


# --- Persistent log --------------------------------------------------------------

class PowerLog:
    """Daily JSON Lines files; finished days are compressed and summarized in index.json."""

    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.index_path = directory / 'index.json'
        try:
            self.index = json.loads(self.index_path.read_text())
            assert isinstance(self.index.get('days'), dict)
        except (OSError, ValueError, AssertionError, AttributeError):
            self.index = {'days': {}}
        self.handle, self.day, self.pending = None, None, 0
        today = self.day_of(time.time())
        for path in sorted(directory.glob('*.jsonl')):
            if path.stem != today:
                self.compress(path)
        for path in sorted(directory.glob('*.jsonl*')):
            if path.suffix not in ('.jsonl', '.zst', '.gz'):
                continue  # An interrupted compression's .part file.
            day = path.name.split('.')[0]
            if day == today or day not in self.index['days']:
                self.index['days'][day] = self.scan(path)

    @staticmethod
    def day_of(wall):
        return datetime.date.fromtimestamp(wall).isoformat()

    @staticmethod
    def scan(path):
        stats = {'records': 0, 'seconds': 0.0, 'first': None, 'last': None}
        try:
            with PowerLog.open(path) as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # A torn final line after a crash.
                    PowerLog.count(stats, record)
        except (OSError, EOFError, ValueError):
            pass  # Includes torn compressed files; the counts so far still stand.
        return stats

    @staticmethod
    def open(path):
        if path.suffix == '.zst' and zstd is not None:
            return zstd.open(path, 'rt')
        return gzip.open(path, 'rt') if path.suffix == '.gz' else open(path)

    @staticmethod
    def count(stats, record):
        if 'event' in record or not isinstance(record.get('dt'), (int, float)):
            return
        stats['records'] += 1
        stats['seconds'] += record['dt']
        stats['first'] = min(filter(None, (stats['first'], record['t0'])))
        stats['last'] = max(filter(None, (stats['last'], record['t1'])))

    def compress(self, path):
        target = path.with_name(path.name + ('.zst' if zstd else '.gz'))
        partial = target.with_name(target.name + '.part')
        with open(path, 'rb') as source, (zstd.open(partial, 'wb', level=10) if zstd
                                          else gzip.open(partial, 'wb')) as sink:
            shutil.copyfileobj(source, sink)
        partial.replace(target)
        path.unlink()

    def write(self, record):
        day = self.day_of(record['t1'])
        if day != self.day:
            if self.handle is not None:
                self.handle.close()
                self.compress(self.directory / f'{self.day}.jsonl')
            self.handle, self.day = open(self.directory / f'{day}.jsonl', 'a'), day
        self.handle.write(json.dumps(record, separators=(',', ':'), ensure_ascii=False) + '\n')
        self.count(self.index['days'].setdefault(
            day, {'records': 0, 'seconds': 0.0, 'first': None, 'last': None}), record)
        self.pending += 1
        if self.pending >= 6 or 'event' in record:
            self.flush()

    def flush(self):
        if self.handle is not None:
            self.handle.flush()
        temporary = self.index_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.index))
        temporary.replace(self.index_path)
        self.pending = 0

    def close(self):
        self.flush()
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def totals(self):
        days = self.index['days'].values()
        firsts = [d['first'] for d in days if d['first']]
        return sum(d['records'] for d in days), sum(d['seconds'] for d in days), min(firsts, default=None)

    def recent(self, since):
        """Records after `since` from today's file (tail only), for warm restarts."""
        path = self.directory / f'{self.day_of(time.time())}.jsonl'
        try:
            with open(path, 'rb') as source:
                source.seek(0, os.SEEK_END)
                offset = max(0, source.tell() - 4 * 2**20)
                source.seek(offset)
                lines = source.read().decode(errors='replace').splitlines()[1 if offset else 0:]
        except OSError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if 'event' not in record and record.get('t1', 0) > since:
                records.append(record)
        return records


# --- Orchestration ---------------------------------------------------------------

def experiment_assignment(path, t0, t1):
    """The running experiment arm for a record, from power-experiment's state file.

    `washout` marks records that began before the arm settled (battery lag,
    governor and link retraining) or that straddle a block change.
    """
    try:
        state = json.loads(path.read_text()) if path else None
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get('status') != 'running':
        return None
    try:
        clean = state['block_start'] <= t0 and t0 >= state['washout_until'] and t1 <= state['block_end'] + 5
        return {'run': state['run'], 'name': state['name'], 'arm': state['arm'],
                'value': state.get('value'), 'block': state['block'], 'washout': not clean}
    except (KeyError, TypeError):
        return None


def boot_id(proc=Path('/proc')):
    return (read(proc / 'sys/kernel/random/boot_id') or '')[:8]


def rounded(name, value):
    if name in ('io_r', 'io_w', 'n'):
        return int(value)
    return round(value) if name == 'rss' else round(value, 4)


def subtract(after, before):
    return {key: round(after[key] - before[key], 4) for key in after
            if key in before and isinstance(after[key], (int, float))}


class Monitor:
    """Called every collector loop; writes a record every RECORD seconds."""

    def __init__(self, directory, sys=Path('/sys'), proc=Path('/proc'), experiment=None):
        self.sys, self.proc, self.experiment = sys, proc, experiment
        self.log = PowerLog(directory)
        self.models_path, self.models_stamp, self.params = directory / 'models.json', None, dict(PRIOR)
        self.refresh_models()
        self.fast, self.idle_states, self.commands = FastSampler(sys), IdleStates(sys), Commands()
        self.gpu, self.io = GpuClients(proc), Deltas()
        self.window = collections.deque()
        now = time.time()
        for record in self.log.recent(now - WINDOW):
            self.window.append((record['t1'], record['dt'], discharging(record),
                                attribute(record, self.params)))
        self.boot = boot_id(proc)
        self.last_battery = None
        self.log.write({'v': SCHEMA, 'event': 'start', 't1': now, 'boot': self.boot})
        self.reset(None, None)
        self.menu = summarize(self.window, now, self.log.totals())

    def reset(self, boot, wall):
        self.start_boot, self.start_wall, self.last_boot = boot, wall, boot
        self.cpu, self.cpu_seen = collections.defaultdict(float), None
        self.frequencies = []
        self.counters = None
        self.fast.reset()

    def snapshot_counters(self):
        return {'cpu': cpu_times(self.proc), 'idle': self.idle_states.read(), 'irq': interrupts(self.proc),
                'net': network(self.sys), 'disk': disks(self.sys)}

    def battery_state(self):
        battery = self.fast.battery
        if battery is None:
            return {}
        result = {'pct': number(battery / 'capacity')}
        for name, unit in (('energy_now', 'uWh'), ('charge_now', 'uAh')):
            if (value := number(battery / name)) is not None:
                result.update(charge=value, unit=unit)
                break
        if (voltage := number(battery / 'voltage_now')) is not None:
            result['volts'] = round(voltage / 1e6, 3)
        return result

    def update(self, boot, wall, processes):
        """`processes`: (pid, start ticks) → (label, cpu seconds, rss bytes, app)."""
        self.fast.sample()
        if self.start_boot is None or boot - self.last_boot > RECORD:
            if self.start_boot is not None:
                self.log.write({'v': SCHEMA, 'event': 'gap', 't0': self.start_wall, 't1': wall,
                                'gap_s': round(boot - self.last_boot, 1), 'boot': self.boot,
                                'bat_before': self.last_battery, 'bat_after': self.battery_state()})
            self.reset(boot, wall)
            self.counters = self.snapshot_counters()
            self.gpu.sample(boot, processes)
            # Re-prime after a gap; the interval spanning the gap is discarded.
            self.io.sample(boot, process_io(self.proc, processes),
                           {key: entry[3] for key, entry in processes.items()})
        if self.cpu_seen is not None:
            for key, entry in processes.items():
                before = self.cpu_seen.get(key)
                if before is not None:
                    self.cpu[entry[3]] += max(0.0, entry[1] - before)
                elif key[1] / TICKS >= self.last_boot:
                    self.cpu[entry[3]] += entry[1]
        self.cpu_seen = {key: entry[1] for key, entry in processes.items()}
        self.last_boot = boot
        mean, peak = cpu_frequency(self.sys)
        if mean is not None:
            self.frequencies.append((mean, peak))
        if boot - self.start_boot >= RECORD:
            record = self.record(boot, wall, processes)
            self.log.write(record)
            self.refresh_models()
            self.window.append((record['t1'], record['dt'], discharging(record),
                                attribute(record, self.params)))
            while self.window and self.window[0][0] < wall - WINDOW:
                self.window.popleft()
            self.start_boot, self.start_wall = boot, wall
            self.cpu = collections.defaultdict(float)
            self.frequencies = []
            self.menu = summarize(self.window, wall, self.log.totals())
        return self.menu

    def record(self, boot, wall, processes):
        dt = boot - self.start_boot
        power, status = self.fast.take()
        counters = self.snapshot_counters()
        before, self.counters = self.counters, counters
        apps_of = {key: entry[3] for key, entry in processes.items()}
        gpu = self.gpu.sample(boot, processes)
        io = self.io.sample(boot, process_io(self.proc, processes), apps_of)
        apps = collections.defaultdict(dict)
        for key, entry in processes.items():
            app = apps[entry[3]]
            app['n'] = app.get('n', 0) + 1
            app['rss'] = app.get('rss', 0) + entry[2] / 2**20
        for name, seconds in self.cpu.items():
            apps[name]['cpu'] = seconds
        for source in (gpu, io):
            for name, values in source.items():
                apps[name].update(values)
        apps = {name: {k: rounded(k, v) for k, v in values.items() if v and not (k == 'rss' and v < 1)}
                for name, values in apps.items()}
        cpu = subtract(counters['cpu'], before['cpu'])
        cpu['busy_s'] = round(sum(cpu.get(k, 0) for k in ('user', 'sys', 'irq', 'steal')), 4)
        if self.frequencies:
            cpu['mhz'] = round(sum(f[0] for f in self.frequencies) / len(self.frequencies))
            cpu['mhz_max'] = round(max(f[1] for f in self.frequencies))
        cpu['idle'] = subtract(counters['idle'], before['idle'])
        irq = subtract(counters['irq'], before['irq'])
        net = {}
        for name, values in counters['net'].items():
            delta = subtract(values, before['net'].get(name, {}))
            if any(delta.values()) or values['state'] == 'up':
                net[name] = dict(delta, state=values['state'])
        disk = {name: subtract(values, before['disk'].get(name, {})) for name, values in counters['disk'].items()}
        slow = self.commands.read(boot)
        shown = display(self.sys)
        if 'niri' in slow:
            shown['niri'] = slow['niri']
        system = settings(self.sys)
        if 'wifi_ps' in slow:
            system['wifi_ps'] = slow['wifi_ps']
        battery = dict(self.battery_state(), **power.get('bat', {}))
        self.last_battery = self.battery_state()
        record = {
            'v': SCHEMA, 't0': round(self.start_wall, 3), 't1': round(wall, 3), 'dt': round(dt, 3),
            'boot': self.boot, 'ac': mains_online(self.sys / 'class/power_supply'), 'status': status,
            'bat': battery, 'soc': power.get('soc', {}),
            'gpu': {'busy': power.get('gpu_busy', {}).get('w', 0),
                    **{k: round(sum(a.get(k, 0) for a in apps.values()), 4) for k in ('gpu', 'video')}},
            'cpu': cpu,
            'irq': dict(sorted(((k, v) for k, v in irq.items() if v > 0), key=lambda r: -r[1])[:12]),
            'apps': apps, 'net': net, 'wifi_dbm': wifi_signal(self.proc), 'disk': disk,
            'usb': usb_active(self.sys), 'pci': pci_active(self.sys), 'audio': audio_streams(self.proc),
            'display': shown, 'settings': system, 'lid': lid_state(self.proc),
            'locked': any(entry[3] == 'swaylock' for entry in processes.values()),
            **thermal(self.sys),
        }
        panel = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'display-panel/state.json'
        try:
            if panel.stat().st_size < 4096:
                record['display']['panel'] = json.loads(panel.read_text())
        except (OSError, ValueError):
            pass
        if (assignment := experiment_assignment(self.experiment, record['t0'], record['t1'])) is not None:
            record['exp'] = assignment
        return record

    def refresh_models(self):
        """Pick up battery-eta's newly trained parameters when models.json changes."""
        try:
            stamp = self.models_path.stat().st_mtime
        except OSError:
            return
        if stamp != self.models_stamp:
            self.models_stamp = stamp
            self.params = load_attribution(self.models_path) or self.params

    def close(self):
        self.log.close()
