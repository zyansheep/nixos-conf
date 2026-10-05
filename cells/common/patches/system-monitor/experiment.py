#!/usr/bin/env python3
"""Randomized A/B ("switchback") battery experiments.

A run alternates a setting between its current value (arm A) and an
alternative (arm B) in blocks of BLOCK seconds, in random order within each
pair, only while on battery. The first WASHOUT seconds of a block are marked
so analysis can skip the battery reading's lag and the system settling.
waybar-monitor tags every power-log record with the running arm (from the state
file below); the battery panel estimates the effect from paired blocks.

    power-experiment list | units | status
    power-experiment start <name> [--minutes N] [--unit SCOPE] [--only-locked]
    power-experiment start freeze-apps [--keep PATTERN ...]   (always only while locked)
    power-experiment stop | restore
    power-experiment next-boot default|psr | boot-status      (boot-level experiments)
    power-experiment test-freeze [--seconds 5] [--keep PATTERN]  (freeze now, measure, thaw)
    power-experiment run      (the power-experiment.service body)

Settings that cannot change at runtime (PSR: amdgpu.dcdebugmask) are boot-level
experiments: the `psr` specialisation boots with PSR enabled, `next-boot` picks
the entry for the next restart only, and the panel compares boots by arm.

Root settings go through `sudo -n power-lab`; its pre-sleep hook restores the
s2idle crash workarounds (ASPM, NVMe APST) before every suspend, and the runner
notices the drift after resume and starts a fresh block.
"""
import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

BLOCK = 240
WASHOUT = 60
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
STATE = RUNTIME / 'experiment.json'
REQUEST = RUNTIME / 'experiment-request.json'
POWER_LAB = ['sudo', '-n', '/run/current-system/sw/bin/power-lab']
SYS = Path('/sys')


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def wifi_interface():
    for iface in sorted((SYS / 'class/net').glob('*')):
        if (iface / 'wireless').exists() or (iface / 'phy80211').exists():
            return iface.name
    return None


def niri_internal():
    outputs = json.loads(run(['niri', 'msg', '-j', 'outputs']) or '{}')
    for output in outputs.values():
        if str(output.get('name', '')).startswith(('eDP', 'LVDS', 'DSI')):
            return output
    raise RuntimeError('no internal display')


def refresh_get():
    output = niri_internal()
    mode = output['modes'][output['current_mode']]
    return f"{mode['width']}x{mode['height']}@{mode['refresh_rate'] / 1000:.3f}"


def refresh_alternative(current):
    output = niri_internal()
    size, _, rate = current.partition('@')
    rates = sorted({m['refresh_rate'] / 1000 for m in output['modes']
                    if f"{m['width']}x{m['height']}" == size}, reverse=True)
    lower = [r for r in rates if r < float(rate) - 1]
    target = lower[0] if lower else (rates[0] if rates else None)
    if target is None or abs(target - float(rate)) < 1:
        raise RuntimeError('panel has a single refresh rate')
    return f'{size}@{target:.3f}'


def aspm_get():
    policy = read(SYS / 'module/pcie_aspm/parameters/policy') or ''
    return next((w[1:-1] for w in policy.split() if w.startswith('[')), policy)


def apst_paths():
    return sorted(SYS.glob('class/nvme/nvme*/power/pm_qos_latency_tolerance_us'))


def cgroup(unit):
    return user_slice() / 'app.slice' / unit


def freeze_get(unit):
    """The kernel's view (cgroup.events), falling back to systemd's FreezerState."""
    events = read(cgroup(unit) / 'cgroup.events')
    if events is not None:
        return 'frozen' if 'frozen 1' in events.split('\n') else 'running'
    state = run(['systemctl', '--user', 'show', unit, '-P', 'FreezerState'], check=False)
    return 'frozen' if state.startswith('frozen') else 'running'


def cpu_usec(unit):
    for line in (read(cgroup(unit) / 'cpu.stat') or '').split('\n'):
        if line.startswith('usage_usec '):
            return int(line.split()[1])
    return None


def freeze_targets(keep):
    """App scopes freeze-apps would freeze, minus those matching `keep`."""
    keep = [k.lower() for k in keep]
    return [u['unit'] for u in freezable_units()
            if not any(k in (u['unit'] + ' ' + u['label']).lower() for k in keep)]


def uncovered(limit=8):
    """The user's busiest processes outside app scopes, which freezing apps cannot reach."""
    groups = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            path = (proc / 'cgroup').read_text().strip().split(':', 2)[2]
            stat = (proc / 'stat').read_text()
            fields = stat[stat.rindex(')') + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
            comm = stat[stat.index('(') + 1:stat.rindex(')')]
        except (OSError, ValueError, IndexError):
            continue
        if '/app.slice/' in path and path.endswith('.scope'):
            continue
        entry = groups.setdefault(path.rsplit('/', 1)[-1], {'cpu_s': 0.0, 'processes': 0, 'by_name': {}})
        seconds = ticks / os.sysconf('SC_CLK_TCK')
        entry['cpu_s'] += seconds
        entry['processes'] += 1
        entry['by_name'][comm] = entry['by_name'].get(comm, 0) + seconds
    ranked = sorted(groups.items(), key=lambda kv: -kv[1]['cpu_s'])[:limit]
    return [{'unit': unit, 'cpu_s': round(info['cpu_s']), 'processes': info['processes'],
             'top': [name for name, _ in sorted(info['by_name'].items(), key=lambda kv: -kv[1])[:2]]}
            for unit, info in ranked]


def freezer_report(units):
    return [{'unit': unit, 'label': unit_label(unit), 'state': freeze_get(unit) if cgroup(unit).is_dir() else 'gone'}
            for unit in units or []]


def test_freeze(units, seconds):
    """Freeze `units` now for `seconds`, measure the CPU they still used, and thaw."""
    before = {unit: cpu_usec(unit) for unit in units}
    try:
        freeze_all_set(units, 'frozen')
        time.sleep(0.5)
        states = {unit: freeze_get(unit) for unit in live_units(units)}
        time.sleep(max(0.0, seconds - 0.5))
        used = {unit: (cpu_usec(unit) or 0) - (before[unit] or 0) for unit in live_units(units)}
    finally:
        freeze_all_set(units, 'running')
    return [{'unit': unit, 'label': unit_label(unit), 'state': states.get(unit, 'gone'),
             'cpu_ms_while_frozen': round(used.get(unit, 0) / 1000)} for unit in units]


def live_units(units):
    return [unit for unit in units if (user_slice() / 'app.slice' / unit).is_dir()]


def freeze_all_get(units):
    alive = live_units(units)
    return 'frozen' if alive and all(freeze_get(unit) == 'frozen' for unit in alive) else 'running'


def freeze_all_set(units, value):
    for unit in live_units(units):  # Apps that exited meanwhile are skipped.
        run(['systemctl', '--user', 'freeze' if value == 'frozen' else 'thaw', unit], check=False)


def psr_arm():
    """Boot-level PSR arm from the running amdgpu.dcdebugmask (bit 0x10 disables PSR)."""
    mask = read(SYS / 'module/amdgpu/parameters/dcdebugmask')
    try:
        return 'default' if int(mask, 0) & 0x10 else 'psr'
    except (TypeError, ValueError):
        return None


class Setting:
    def __init__(self, name, label, description, get, set, alternative, root=False, visible='',
                 sleep_restored=False, unit=None, ac_managed=False, units=None):
        self.name, self.label, self.description = name, label, description
        self.get, self.set, self.alternative = get, set, alternative
        self.root, self.visible, self.sleep_restored, self.unit = root, visible, sleep_restored, unit
        # udev switches these on AC/battery changes; never fight it while plugged in.
        self.ac_managed = ac_managed
        self.units = units


def catalog(unit=None, units=None):
    """Experiment specs; `unit` / `units` parameterize the app-freeze experiments."""
    wifi = wifi_interface()
    specs = [
        Setting('aspm', 'PCIe link power saving (ASPM)',
                'Lets PCIe links (NVMe, Wi-Fi, GPU) sleep. Disabled at boot as an s2idle crash workaround; '
                'restored before every suspend.',
                aspm_get, lambda v: run(POWER_LAB + ['aspm', v]),
                lambda cur: 'powersupersave' if cur != 'powersupersave' else 'performance',
                root=True, sleep_restored=True),
        Setting('apst', 'NVMe autonomous power states (APST)',
                'Lets the SSD drop into low-power states when idle. Disabled at boot as an s2idle crash '
                'workaround; restored before every suspend.',
                lambda: read(apst_paths()[0]) if apst_paths() else None,
                lambda v: run(POWER_LAB + ['apst', v]),
                lambda cur: '100000' if cur == '0' else '0', root=True, sleep_restored=True),
        Setting('boost', 'CPU boost', 'Clocks above base frequency for short bursts.',
                lambda: read(SYS / 'devices/system/cpu/cpufreq/boost'),
                lambda v: run(POWER_LAB + ['boost', v]),
                lambda cur: '0' if cur == '1' else '1', root=True, visible='Bursty work gets slower.'),
        Setting('abm', 'Panel adaptive backlight (ABM)',
                'Dims the backlight and boosts pixel values to compensate.',
                lambda: read(next(iter(sorted(SYS.glob('class/drm/card*-eDP-*/amdgpu/panel_power_savings'))), '/nonexistent')),
                lambda v: run(POWER_LAB + ['abm', v]),
                lambda cur: '0' if cur not in (None, '0') else '3', root=True, visible='Slight contrast change.',
                ac_managed=True),
        Setting('profile', 'Power profile', 'power-profiles-daemon profile (platform profile and EPP).',
                lambda: run(['powerprofilesctl', 'get']),
                lambda v: run(['powerprofilesctl', 'set', v]),
                lambda cur: 'balanced' if cur == 'power-saver' else 'power-saver',
                visible='Responsiveness changes.', ac_managed=True),
        Setting('refresh', 'Display refresh rate', 'Lower internal-panel refresh rate.',
                refresh_get, lambda v: run(['niri', 'msg', 'output', niri_internal()['name'], 'mode', v]),
                refresh_alternative, visible='Motion looks slightly less smooth.'),
    ]
    if wifi:
        specs.append(Setting(
            'wifi-ps', 'Wi-Fi power saving', 'Radio sleeps between beacons (adds latency).',
            lambda: run(['iw', 'dev', wifi, 'get', 'power_save']).split(':')[-1].strip(),
            lambda v: run(POWER_LAB + ['wifips', v]),
            lambda cur: 'off' if cur == 'on' else 'on', root=True, visible='Network latency changes.'))
    if unit:
        specs.append(Setting(
            'freeze', f'Close {unit_label(unit)}', 'Freezes the app (cgroup freezer) to measure its cost.',
            lambda: freeze_get(unit),
            lambda v: run(['systemctl', '--user', 'freeze' if v == 'frozen' else 'thaw', unit]),
            lambda cur: 'frozen', visible='The app stops responding during B blocks.', unit=unit))
    if units:
        specs.append(Setting(
            'freeze-apps', f'Close every app ({len(units)} scopes)',
            'Freezes all app scopes while the screen is locked: frozen blocks measure the platform floor, '
            'and the difference is what the apps cost in the background.',
            lambda: freeze_all_get(units), lambda v: freeze_all_set(units, v), lambda cur: 'frozen',
            visible='Only runs while the screen is locked.', units=units))
    return {spec.name: spec for spec in specs}


def unit_label(unit):
    match = re.match(r'app-(?:niri-|gnome-|flatpak-)?(.+?)(?:-\d+)?\.scope$', unit)
    if match:
        return match.group(1).replace('\\x2d', '-')
    cgroup = user_slice() / 'app.slice' / unit
    try:
        pid = (cgroup / 'cgroup.procs').read_text().split()[0]
        return Path(f'/proc/{pid}/comm').read_text().strip()
    except (OSError, IndexError):
        return unit


def user_slice():
    return Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service')


def freezable_units():
    """App scopes by total CPU time (largest first), for the panel's picker."""
    units = []
    for scope in (user_slice() / 'app.slice').glob('*.scope'):
        try:
            usage = int((scope / 'cpu.stat').read_text().split('\n')[0].split()[1])
            # populated covers sub-cgroups too (Flatpak apps live one level down).
            if 'populated 1' not in (scope / 'cgroup.events').read_text().split('\n'):
                continue
        except (OSError, IndexError, ValueError):
            continue
        units.append({'unit': scope.name, 'label': unit_label(scope.name), 'cpu_s': round(usage / 1e6)})
    return sorted(units, key=lambda u: -u['cpu_s'])


def on_battery():
    for supply in (SYS / 'class/power_supply').glob('*'):
        if read(supply / 'type') == 'Mains' and read(supply / 'online') == '1':
            return False
    return any(read(s / 'type') == 'Battery' and read(s / 'status') == 'Discharging'
               for s in (SYS / 'class/power_supply').glob('*'))


def screen_locked():
    return subprocess.run(['pgrep', '-x', 'swaylock'], capture_output=True).returncode == 0


def write_state(state, path=None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


def schedule(rng):
    """Arms in randomly ordered A/B pairs, so interruptions stay roughly balanced."""
    while True:
        pair = ['A', 'B']
        rng.shuffle(pair)
        yield from pair


class Clock:
    """Real time; tests substitute a fake."""
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)  # Shadows the module inside this class body only.

    @staticmethod
    def suspended_since(mark):
        # BOOTTIME advances through suspend, MONOTONIC does not.
        now = (time.clock_gettime(time.CLOCK_BOOTTIME), time.clock_gettime(time.CLOCK_MONOTONIC))
        return mark is not None and (now[0] - mark[0]) - (now[1] - mark[1]) > 2, now


def experiment(spec, minutes, only_locked=False, block=BLOCK, washout=WASHOUT, clock=Clock,
               battery=on_battery, locked=screen_locked, state_path=None, rng=None):
    original = spec.get()
    if original is None:
        raise RuntimeError(f'{spec.name}: setting is not available here')
    arms = {'A': original, 'B': spec.alternative(original)}
    started = clock.time()
    base = {'run': f'{spec.name}-{int(started)}', 'name': spec.name, 'label': spec.label,
            'arms': arms, 'original': original, 'unit': spec.unit, 'units': spec.units, 'started': started,
            'ends_at': started + minutes * 60, 'block_seconds': block, 'washout_seconds': washout}
    arms_order = schedule(rng or random.Random())
    number, mark = 0, None

    def restore():
        if spec.ac_managed and not battery():
            return  # udev already applied the AC value.
        if spec.get() != original:
            spec.set(original)
    try:
        while clock.time() < base['ends_at']:
            reason = ('on AC power' if not battery() else
                      'waiting for the screen to lock' if only_locked and not locked() else None)
            if reason:
                restore()
                write_state(dict(base, status='paused', reason=reason, blocks=number), state_path)
                clock.sleep(15)
                continue
            arm = next(arms_order)
            if spec.get() != arms[arm]:
                spec.set(arms[arm])
            begin = clock.time()
            number += 1
            state = dict(base, status='running', block=number, arm=arm, value=arms[arm], block_start=begin,
                         washout_until=begin + washout, block_end=begin + block, blocks=number)
            write_state(state, state_path)
            _, mark = clock.suspended_since(None)
            while clock.time() < state['block_end'] and clock.time() < base['ends_at']:
                clock.sleep(5)
                suspended, mark = clock.suspended_since(mark)
                interrupted = (suspended or not battery() or (only_locked and not locked())
                               or spec.get() != arms[arm])
                if interrupted:
                    break
            if clock.time() < state['block_end']:
                write_state(dict(state, block_end=clock.time()), state_path)  # Ended early.
    finally:
        try:
            restore()
        finally:
            write_state(dict(base, status='finished', finished=clock.time(), blocks=number), state_path)


def command_run():
    request = json.loads(REQUEST.read_text())
    spec = catalog(request.get('unit'), request.get('units'))[request['name']]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Freezing every app is only acceptable while nobody is using them.
    only_locked = request.get('only_locked', False) or spec.name == 'freeze-apps'
    experiment(spec, request.get('minutes', 60), only_locked)


def command_restore():
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return
    if state.get('status') in ('running', 'paused'):
        spec = catalog(state.get('unit'), state.get('units')).get(state['name'])
        if spec and spec.get() != state['original']:
            spec.set(state['original'])
        write_state(dict(state, status='finished', finished=time.time()))


def describe(spec):
    try:
        current = spec.get()
        alternative = spec.alternative(current) if current is not None else None
        error = None if current is not None else 'not available'
    except Exception as exc:  # noqa: BLE001 - shown to the user, never fatal
        current = alternative = None
        error = str(exc)
    return {'name': spec.name, 'label': spec.label, 'description': spec.description, 'root': spec.root,
            'visible': spec.visible, 'sleep_restored': spec.sleep_restored, 'current': current,
            'alternative': alternative, 'error': error}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='power-experiment', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('units')
    sub.add_parser('status')
    start = sub.add_parser('start')
    start.add_argument('name')
    start.add_argument('--minutes', type=int, default=60)
    start.add_argument('--unit')
    start.add_argument('--only-locked', action='store_true')
    start.add_argument('--keep', action='append',
                       help='freeze-apps: leave scopes matching this (default: t3code, so agents keep running)')
    sub.add_parser('stop')
    boot = sub.add_parser('next-boot')
    boot.add_argument('arm', choices=['default', 'psr'])
    sub.add_parser('boot-status')
    test = sub.add_parser('test-freeze', help='freeze the freeze-apps targets now for a few seconds, then thaw')
    test.add_argument('--seconds', type=float, default=5)
    test.add_argument('--keep', action='append')
    sub.add_parser('restore')
    sub.add_parser('run')
    args = parser.parse_args(argv)
    if args.command == 'list':
        print(json.dumps([describe(spec) for spec in catalog().values()]))
    elif args.command == 'units':
        print(json.dumps(freezable_units()))
    elif args.command == 'status':
        try:
            state = json.loads(read(STATE) or '{}')
        except ValueError:
            state = {}
        state.update(locked=screen_locked(), on_battery=on_battery(), freezer=freezer_report(state.get('units')),
                     uncovered=uncovered())
        print(json.dumps(state, indent=1))
    elif args.command == 'test-freeze':
        units = freeze_targets(args.keep if args.keep is not None else ['t3code'])
        print(json.dumps({'seconds': args.seconds, 'units': test_freeze(units, args.seconds),
                          'uncovered': uncovered()}, indent=1))
    elif args.command == 'start':
        units = None
        if args.name == 'freeze-apps':
            units = freeze_targets(args.keep if args.keep is not None else ['t3code'])
            if not units:
                parser.error('no app scopes to freeze')
        if args.name not in catalog(args.unit, units):
            parser.error(f'unknown experiment {args.name!r}' + (' (needs --unit)' if args.name == 'freeze' else ''))
        write_state({'name': args.name, 'minutes': max(8, min(args.minutes, 600)), 'unit': args.unit,
                     'units': units, 'only_locked': args.only_locked}, REQUEST)
        run(['systemctl', '--user', 'restart', 'power-experiment.service'])
    elif args.command == 'stop':
        run(['systemctl', '--user', 'stop', 'power-experiment.service'], check=False)
    elif args.command == 'restore':
        command_restore()
    elif args.command == 'next-boot':
        print(run(POWER_LAB + ['next-boot', args.arm]))
    elif args.command == 'boot-status':
        print(json.dumps({'psr': psr_arm()}))
    elif args.command == 'run':
        command_run()


if __name__ == '__main__':
    if shutil.which('systemctl') is None:
        sys.exit('power-experiment needs systemd')
    main()
