#!/usr/bin/env python3
"""Bounded, local cache for Waybar's native hover panels; no command lines stored."""
import collections
import json
import os
from pathlib import Path
import time

TICKS = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')


APP_NAMES = {
    'floorp': 'Floorp', 'firefox': 'Firefox', 'chromium': 'Chromium',
    'chrome': 'Chrome', 'chatgpt': 'ChatGPT', 'codium': 'VSCodium',
    'code': 'VS Code', 'vesktop': 'Vesktop', 'vesktop.bin': 'Vesktop', 'obsidian': 'Obsidian',
    't3code': 'T3 Code', 'electron': 'Electron', 'niri': 'Niri',
    'waybar': 'Waybar', 'swaync': 'Notifications',
    'nm-sidebar-gui': 'Wi-Fi panel', 'waybar-monitor': 'System monitor',
    'pipewire': 'PipeWire', 'pipewire-pulse': 'PipeWire audio',
    'wireplumber': 'WirePlumber', 'cc1': 'C compiler', 'cc1plus': 'C++ compiler',
}
BROWSERS = {'Floorp', 'Firefox', 'Chromium', 'Chrome'}


def executable_name(value):
    name = Path(value).name.removesuffix(' (deleted)').lstrip('.')
    return name.removesuffix('-wrapped')


def process_identity(directory, comm):
    """Read only identity metadata. Arguments are never retained in the cache."""
    try:
        exe = executable_name(str((directory / 'exe').readlink()))
    except OSError:
        exe = executable_name(comm)
    app = APP_NAMES.get(exe.lower())
    args = []
    if exe.lower().startswith('electron') or app in {'Chromium', 'Chrome'}:
        try:
            with (directory / 'cmdline').open('rb') as command:
                args = command.read(16384).decode(errors='replace').split('\0')
        except OSError:
            pass
    # Electron shares a binary across apps. Recognize installed app entry paths,
    # never URLs or arbitrary substring matches in document/file arguments.
    if exe.lower().startswith('electron'):
        app = APP_NAMES.get(comm.lower(), 'Electron')
        for arg in args[:4]:
            if arg.startswith('/') and arg.endswith(('.asar', '.js')):
                parts = {part.lower() for part in Path(arg).parts}
                for key in ('vesktop', 'obsidian', 't3code', 'codium'):
                    if key in parts:
                        app = APP_NAMES[key]
                        break
    role = next((a.partition('=')[2] for a in args if a.startswith('--type=')), '')
    return app, role


def process_label(pid, comm, app, role):
    if app in BROWSERS:
        if comm.startswith(('Isolated Web', 'Web Content', 'WebCOOP')) or role == 'renderer':
            # One process may host several tabs or cross-site frames. A PID is
            # honest and can be matched to the browser's about:processes page.
            return f'{app} web · PID {pid}'
        if comm.startswith('Isolated Servic'):
            return f'{app} worker · PID {pid}'
        roles = {'GPU Process': 'graphics', 'WebExtensions': 'extensions', 'RDD Process': 'media decoder',
                 'Socket Process': 'network', 'Utility Process': 'utilities',
                 'Privileged Cont': 'browser pages', 'Privileged Mozi': 'browser services',
                 'Web Content': 'web content'}
        if comm in roles:
            return f'{app} · {roles[comm]}'
    if role:
        readable = {'renderer': 'renderer', 'gpu-process': 'GPU',
                    'utility': 'utilities', 'zygote': 'process launcher'}.get(role, 'helper')
        return f'{app or executable_name(comm)} · {readable}'
    return app or APP_NAMES.get(executable_name(comm).lower(), executable_name(comm))


def processes(root=Path('/proc')):
    result = {}
    metadata = {}
    for directory in root.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            stat = (directory / 'stat').read_text()
            # comm may contain spaces and parentheses; fields after its final ')'
            # begin with field 3 (state).
            end = stat.rindex(')')
            fields = stat[end + 2:].split()
            name = stat[stat.index('(') + 1:end]
            start = int(fields[19])
            app, role = process_identity(directory, name)
            metadata[int(directory.name)] = (int(fields[1]), app, role, name)
            result[(int(directory.name), start)] = (
                name, (int(fields[11]) + int(fields[12])) / TICKS,
                max(0, int(fields[21])) * PAGE)
        except (OSError, ValueError, IndexError):
            continue  # Exited process or inaccessible task; never fail the sample.
    def application(pid, seen=None):
        seen = set() if seen is None else seen
        if pid in seen or pid not in metadata or len(seen) > 12:
            return None
        seen.add(pid)
        parent, app, role, _ = metadata[pid]
        if app is None or (app == 'Electron' and role):
            ancestor = application(parent, seen)
            # Sandboxed user services may read stat but not /proc/PID/exe.
            # Browser ancestry still identifies content/helper processes.
            if ancestor in BROWSERS or (app == 'Electron' and role):
                return ancestor or app
        return app

    for key, (comm, seconds, rss) in list(result.items()):
        pid = key[0]
        result[key] = (process_label(pid, comm, application(pid), metadata[pid][2]), seconds, rss)
    return result


def temperatures(hwmon=Path('/sys/class/hwmon'), thermal=Path('/sys/class/thermal')):
    result = []
    for directory in sorted(hwmon.glob('hwmon*')):
        try:
            chip = (directory / 'name').read_text().strip()
        except OSError:
            chip = directory.name
        for sensor in sorted(directory.glob('temp*_input')):
            try:
                value = float(sensor.read_text()) / 1000
                label = sensor.with_name(sensor.name.replace('_input', '_label'))
                name = label.read_text().strip() if label.exists() else sensor.stem.replace('_input', '')
                if -273.15 < value < 300:
                    result.append({'name': f'{chip} · {name}', 'value': f'{value:.1f} °C'})
            except (OSError, ValueError):
                continue
    for zone in sorted(thermal.glob('thermal_zone*')):
        try:
            value = float((zone / 'temp').read_text()) / 1000
            if -273.15 < value < 300:
                result.append({'name': f"{(zone / 'type').read_text().strip()} · {zone.name}",
                               'value': f'{value:.1f} °C'})
        except (OSError, ValueError):
            continue
    return result


class History:
    def __init__(self, cores=None):
        self.cores = cores or os.cpu_count() or 1
        self.previous = None
        self.last_time = None
        self.intervals = collections.deque()

    def sample(self, now, current):
        if self.previous is not None:
            elapsed = now - self.last_time
            if 0 < elapsed <= 10:
                usage = collections.defaultdict(float)
                for key, (name, seconds, _) in current.items():
                    if key in self.previous:
                        usage[name] += max(0, seconds - self.previous[key][1])
                    elif key[1] / TICKS >= self.last_time:
                        usage[name] += seconds
                self.intervals.append((self.last_time, now, usage))
            else:
                self.intervals.clear()  # Suspend or clock discontinuity.
        self.previous, self.last_time = current, now
        cutoff = now - 60
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        usage = collections.defaultdict(float)
        covered = 0.0
        for start, end, values in self.intervals:
            duration = end - max(start, cutoff)
            covered += duration
            for name, seconds in values.items():
                usage[name] += seconds * duration / (end - start)
        memory = collections.defaultdict(int)
        for name, _, rss in current.values():
            memory[name] += rss
        cpu = [{'name': name, 'value': f'{seconds / covered / self.cores * 100:.1f}%'}
               for name, seconds in sorted(usage.items(), key=lambda row: (-row[1], row[0]))[:100]
               if covered and seconds > 0]
        ram = [{'name': name, 'value': f'{rss / 2**20:,.0f} MiB'}
               for name, rss in sorted(memory.items(), key=lambda row: (-row[1], row[0]))[:100]
               if rss]
        return {'cpu': cpu, 'memory': ram, 'coverage': round(covered), 'timestamp': time.time()}


def browser_labels(state, current, directory, now=None):
    """Enrich display rows after accounting; changing titles cannot split CPU history."""
    now = time.time() if now is None else now
    labels = {}
    for path in directory.glob('*.json'):
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            data = json.loads(path.read_text())
            if abs(now - float(data['timestamp'])) > 15:
                continue
            for row in data['processes'][:2048]:
                key = (int(row['pid']), int(row['start']))
                process = current.get(key)
                if not process or not process[0].startswith(('Floorp web · PID ', 'Floorp worker · PID ')):
                    continue
                origin = str(row['origin']).split('^')[0]
                if not origin:
                    continue
                title = next((t for t in row.get('titles', []) if isinstance(t, str) and t), '')
                detail = origin + (' · ' + title if title else '')
                detail = ' '.join(detail.split())[:160]
                labels[process[0]] = f'Floorp · {detail} · PID {key[0]}'
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    for kind in ('cpu', 'memory'):
        for row in state[kind]:
            row['name'] = labels.get(row['name'], row['name'])


def main():
    directory = Path(os.environ['XDG_RUNTIME_DIR']) / 'waybar-monitor'
    directory.mkdir(mode=0o700, exist_ok=True)
    history = History()
    while True:
        began = time.clock_gettime(time.CLOCK_BOOTTIME)
        state = history.sample(began, processes())
        browser_labels(state, history.previous, directory.parent / 'floorp-monitor')
        state['temperature'] = temperatures()
        temporary = directory / 'snapshot.tmp'
        temporary.write_text(json.dumps(state, ensure_ascii=True))
        temporary.chmod(0o600)
        temporary.replace(directory / 'snapshot.json')
        elapsed = time.clock_gettime(time.CLOCK_BOOTTIME) - began
        time.sleep(max(.1, 2 - elapsed))


if __name__ == '__main__':
    main()
