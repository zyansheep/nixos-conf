#!/usr/bin/env python3
"""Bounded, local cache for Waybar's native hover panels; no command lines stored."""
import collections
import json
import os
from pathlib import Path
import time

TICKS = os.sysconf('SC_CLK_TCK')
PAGE = os.sysconf('SC_PAGE_SIZE')


def processes(root=Path('/proc')):
    result = {}
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
            result[(int(directory.name), start)] = (
                name, (int(fields[11]) + int(fields[12])) / TICKS,
                max(0, int(fields[21])) * PAGE)
        except (OSError, ValueError, IndexError):
            continue  # Exited process or inaccessible task; never fail the sample.
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
               for name, seconds in sorted(usage.items(), key=lambda row: (-row[1], row[0]))[:20]
               if covered and seconds > 0]
        ram = [{'name': name, 'value': f'{rss / 2**20:,.0f} MiB'}
               for name, rss in sorted(memory.items(), key=lambda row: (-row[1], row[0]))[:20]
               if rss]
        return {'cpu': cpu, 'memory': ram, 'coverage': round(covered), 'timestamp': time.time()}


def main():
    directory = Path(os.environ['XDG_RUNTIME_DIR']) / 'waybar-monitor'
    directory.mkdir(mode=0o700, exist_ok=True)
    history = History()
    while True:
        began = time.clock_gettime(time.CLOCK_BOOTTIME)
        state = history.sample(began, processes())
        state['temperature'] = temperatures()
        temporary = directory / 'snapshot.tmp'
        temporary.write_text(json.dumps(state, ensure_ascii=True))
        temporary.chmod(0o600)
        temporary.replace(directory / 'snapshot.json')
        elapsed = time.clock_gettime(time.CLOCK_BOOTTIME) - began
        time.sleep(max(.1, 2 - elapsed))


if __name__ == '__main__':
    main()
