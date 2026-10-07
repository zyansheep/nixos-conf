#!/usr/bin/env python3
"""Randomized A/B ("switchback") battery experiments.

A run randomizes one or more settings, each between its current value (arm
A) and an alternative (arm B), in blocks of BLOCK seconds, only while on
battery. Several settings run as a factorial design (see `design`): every block
informs every setting at once, and settings randomized together reveal whether
they interact. The first WASHOUT seconds of a block are marked so analysis can
skip the battery reading's lag and the system settling. waybar-monitor tags
every record with the running arms (from the state file below); the battery
panel's Savings tab fits all settings' effects together (report.lever_effects).

    power-experiment list | units | status
    power-experiment enable | disable          (automatic experiments, see `automatic`)
    power-experiment pool [<name> ...]         (the settings they may vary)
    power-experiment start <name> [<name> ...] [--minutes N] [--unit SCOPE] [--only-locked]
    power-experiment apply <name>=<value> ...   (set now, e.g. the panel's best combination)
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
import itertools
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

import qol

BLOCK = 240
WASHOUT = 60
GIVE_UP = 7 * 86400  # A run that cannot collect its battery time in a week ends.
# Automatic experiments: on while ENABLED exists (power-experiments.service's
# condition), varying the settings listed in POOL.
SAVED = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'waybar-monitor'
ENABLED, POOL = SAVED / 'experiments-enabled', SAVED / 'experiments.json'
AUTO_UNIT = 'power-experiments.service'
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


def refresh_levels():
    """(lowest, highest) refresh mode of the internal panel at its current size."""
    output = niri_internal()
    current = output['modes'][output['current_mode']]
    modes = sorted((m for m in output['modes'] if (m['width'], m['height']) == (current['width'], current['height'])),
                   key=lambda m: m['refresh_rate'])
    mode = lambda m: f"{m['width']}x{m['height']}@{m['refresh_rate'] / 1000:.3f}"
    return mode(modes[0]), mode(modes[-1])


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
                 sleep_restored=False, unit=None, ac_managed=False, units=None, levels=None):
        self.name, self.label, self.description = name, label, description
        self.get, self.set, self.alternative = get, set, alternative
        # () → (power-saving value, normal value), for `apply name=saving|normal`.
        self.levels = levels
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
                root=True, sleep_restored=True, levels=lambda: ('powersupersave', 'performance')),
        Setting('apst', 'NVMe autonomous power states (APST)',
                'Lets the SSD drop into low-power states when idle. Disabled at boot as an s2idle crash '
                'workaround; restored before every suspend.',
                lambda: read(apst_paths()[0]) if apst_paths() else None,
                lambda v: run(POWER_LAB + ['apst', v]),
                lambda cur: '100000' if cur == '0' else '0', root=True, sleep_restored=True,
                levels=lambda: ('100000', '0')),
        Setting('boost', 'CPU boost', 'Clocks above base frequency for short bursts.',
                lambda: read(SYS / 'devices/system/cpu/cpufreq/boost'),
                lambda v: run(POWER_LAB + ['boost', v]),
                lambda cur: '0' if cur == '1' else '1', root=True, visible='Bursty work gets slower.',
                levels=lambda: ('0', '1')),
        Setting('abm', 'Panel adaptive backlight (ABM)',
                'Dims the backlight and boosts pixel values to compensate.',
                lambda: read(next(iter(sorted(SYS.glob('class/drm/card*-eDP-*/amdgpu/panel_power_savings'))), '/nonexistent')),
                lambda v: run(POWER_LAB + ['abm', v]),
                lambda cur: '0' if cur not in (None, '0') else '3', root=True, visible='Slight contrast change.',
                ac_managed=True, levels=lambda: ('3', '0')),
        Setting('profile', 'Power profile', 'power-profiles-daemon profile (platform profile and EPP).',
                lambda: run(['powerprofilesctl', 'get']),
                lambda v: run(['powerprofilesctl', 'set', v]),
                lambda cur: 'balanced' if cur == 'power-saver' else 'power-saver',
                visible='Responsiveness changes.', ac_managed=True, levels=lambda: ('power-saver', 'balanced')),
        Setting('refresh', 'Display refresh rate', 'Lower internal-panel refresh rate.',
                refresh_get, lambda v: run(['niri', 'msg', 'output', niri_internal()['name'], 'mode', v]),
                refresh_alternative, visible='Motion looks slightly less smooth.', levels=refresh_levels),
    ]
    if wifi:
        specs.append(Setting(
            'wifi-ps', 'Wi-Fi power saving', 'Radio sleeps between beacons (adds latency).',
            lambda: run(['iw', 'dev', wifi, 'get', 'power_save']).split(':')[-1].strip(),
            lambda v: run(POWER_LAB + ['wifips', v]),
            lambda cur: 'off' if cur == 'on' else 'on', root=True, visible='Network latency changes.',
            levels=lambda: ('on', 'off')))
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


def design(names, rng):
    """Arms per block, {name: 'A' | 'B'}, in shuffled cycles.

    Up to four settings: every combination once per cycle (a full factorial,
    so each setting is B in half the blocks and pairs separate cleanly). More:
    the first four are crossed and every other setting is B in a random half
    of each cycle. One setting is plain A/B pairs in random order.
    """
    crossed, rest = names[:4], names[4:]
    while True:
        combos = [dict(zip(crossed, arms)) for arms in itertools.product('AB', repeat=len(crossed))]
        for name in rest:
            column = ['A', 'B'] * (len(combos) // 2)
            rng.shuffle(column)
            for combo, arm in zip(combos, column):
                combo[name] = arm
        rng.shuffle(combos)
        yield from combos


class Clock:
    """Real time; tests substitute a fake."""
    sleep = staticmethod(time.sleep)
    time = staticmethod(time.time)  # Shadows the module inside this class body only.

    @staticmethod
    def suspended_since(mark):
        # BOOTTIME advances through suspend, MONOTONIC does not.
        now = (time.clock_gettime(time.CLOCK_BOOTTIME), time.clock_gettime(time.CLOCK_MONOTONIC))
        return mark is not None and (now[0] - mark[0]) - (now[1] - mark[1]) > 2, now


def experiment(specs, minutes, only_locked=False, block=BLOCK, washout=WASHOUT, clock=Clock,
               battery=on_battery, locked=screen_locked, state_path=None, rng=None):
    """Randomize `specs` (one Setting or several) together in blocks until `minutes`
    of battery time are collected (AC and, with `only_locked`, unlocked time don't count)."""
    specs = [specs] if isinstance(specs, Setting) else list(specs)
    names = [spec.name for spec in specs]
    originals = {}
    for spec in specs:
        if (originals.setdefault(spec.name, spec.get())) is None:
            raise RuntimeError(f'{spec.name}: setting is not available here')
    arms = {spec.name: {'A': originals[spec.name], 'B': spec.alternative(originals[spec.name])} for spec in specs}
    started = clock.time()
    # `name`, `arm` and `value` are what waybar-monitor tags records with:
    # 'aspm+apst', 'BA' and {'aspm': …, 'apst': …} (a single setting: 'B' and its value).
    base = {'run': f"{'+'.join(names)}-{int(started)}", 'name': '+'.join(names), 'names': names,
            'label': ' + '.join(spec.label for spec in specs), 'arms': arms, 'originals': originals,
            'unit': specs[0].unit, 'units': specs[0].units, 'started': started, 'target_seconds': minutes * 60,
            'gives_up_at': started + GIVE_UP, 'block_seconds': block, 'washout_seconds': washout}
    configs = design(names, rng or random.Random())
    number, mark, collected = 0, None, 0.0

    def restore():
        failures = []
        for spec in specs:
            if spec.ac_managed and not battery():
                continue  # udev already applied the AC value.
            try:
                if spec.get() != originals[spec.name]:
                    spec.set(originals[spec.name])
            except Exception as error:  # noqa: BLE001 - restore the rest first
                failures.append(error)
        if failures:
            raise failures[0]
    try:
        while collected < base['target_seconds'] and clock.time() < base['gives_up_at']:
            reason = ('on AC power' if not battery() else
                      'waiting for the screen to lock' if only_locked and not locked() else None)
            if reason:
                restore()
                write_state(dict(base, status='paused', reason=reason, blocks=number, collected=collected), state_path)
                clock.sleep(15)
                continue
            config = next(configs)
            values = {name: arms[name][config[name]] for name in names}
            for spec in specs:
                if spec.get() != values[spec.name]:
                    spec.set(values[spec.name])
            begin = clock.time()
            number += 1
            state = dict(base, status='running', block=number, config=config,
                         arm=''.join(config[name] for name in names),
                         value=values[names[0]] if len(names) == 1 else values, block_start=begin,
                         washout_until=begin + washout, block_end=begin + min(block, base['target_seconds'] - collected),
                         blocks=number, collected=collected)
            write_state(state, state_path)
            _, mark = clock.suspended_since(None)
            while clock.time() < state['block_end']:
                clock.sleep(5)
                suspended, mark = clock.suspended_since(mark)
                interrupted = (suspended or not battery() or (only_locked and not locked())
                               or any(spec.get() != values[spec.name] for spec in specs))
                if interrupted:
                    break
            collected += clock.time() - begin
            if clock.time() < state['block_end']:
                write_state(dict(state, block_end=clock.time(), collected=collected), state_path)  # Ended early.
    finally:
        try:
            restore()
        finally:
            write_state(dict(base, status='finished', finished=clock.time(), blocks=number, collected=collected),
                        state_path)


def pool(specs=None):
    """Settings automatic experiments may vary (all with power-saving/normal values by default)."""
    specs = specs if specs is not None else catalog()
    try:
        names = json.loads(POOL.read_text())['settings']
    except (OSError, ValueError, KeyError, TypeError):
        names = [name for name, spec in specs.items() if spec.levels]
    return [name for name in names if name in specs and specs[name].levels]


def next_block(names, present):
    """report.plan's next combination (numpy: only the automatic runner needs it)."""
    import report
    now = time.time()
    minutes, events = report.load_range(now - 30 * 86400, now + 60)
    plan = report.plan(minutes, events, names, present=present)
    return plan['choice'], {'accuracy': plan['accuracy'], 'allowed': plan['allowed'],
                            'combinations': plan['combinations']}


def automatic(specs=None, clock=Clock, battery=on_battery, locked=screen_locked, resting=qol.resting,
              enabled=ENABLED.exists, names=pool, choose=next_block, block=BLOCK, washout=WASHOUT, state_path=None):
    """Automatic experiments, for as long as they are enabled: each block, the
    combination of the pool's settings report.plan expects to sharpen the
    battery model most, among those unlikely to earn a 👎 while you are there.
    Pauses (restoring the settings) on AC, and for qol.COOLDOWN after a 👎 (a
    👍 resumes); a 👎 ends the block at once."""
    specs = specs if specs is not None else catalog()
    originals, number, mark = {}, 0, None
    started = clock.time()
    run_id = f'auto-{int(started)}'

    def restore(names=None):
        failures = []
        for name in list(names if names is not None else originals):
            spec = specs[name]
            if spec.ac_managed and not battery():
                continue  # udev already applied the AC value.
            try:
                if spec.get() != originals[name]:
                    spec.set(originals[name])
            except Exception as error:  # noqa: BLE001 - restore the rest first
                failures.append(error)
        if failures:
            raise failures[0]
    try:
        while enabled():
            current = names(specs)
            for name in current:
                originals.setdefault(name, specs[name].get())
            restore([name for name in originals if name not in current])  # Dropped from the pool.
            base = {'run': run_id, 'mode': 'auto', 'name': '+'.join(current), 'names': current,
                    'label': 'Automatic experiments', 'originals': originals, 'started': started,
                    'block_seconds': block, 'washout_seconds': washout, 'blocks': number}
            here = not locked()
            reason = ('no settings to test' if not current else 'on AC power' if not battery() else
                      'resting after your 👎 (👍 resumes)' if resting() > 0 else None)
            if reason:
                restore()
                write_state(dict(base, status='paused', reason=reason), state_path)
                clock.sleep(15)
                continue
            choice, plan = choose(current, here)
            values = {name: specs[name].levels()[0 if choice[name] else 1] for name in current}
            for name in current:
                if specs[name].get() != values[name]:
                    specs[name].set(values[name])
            begin = clock.time()
            number += 1
            state = dict(base, status='running', block=number, blocks=number, plan=plan,
                         config={name: 'B' if choice[name] else 'A' for name in current},
                         arm=''.join('B' if choice[name] else 'A' for name in current), value=values,
                         block_start=begin, washout_until=begin + washout, block_end=begin + block)
            write_state(state, state_path)
            _, mark = clock.suspended_since(None)
            while clock.time() < state['block_end']:
                clock.sleep(5)
                suspended, mark = clock.suspended_since(mark)
                if (suspended or not battery() or not enabled() or names(specs) != current or resting() > 0
                        or any(specs[name].get() != values[name] for name in current)):
                    break
            if clock.time() < state['block_end']:
                write_state(dict(state, block_end=clock.time()), state_path)  # Ended early.
    finally:
        try:
            restore()
        finally:
            write_state({'run': run_id, 'mode': 'auto', 'label': 'Automatic experiments', 'status': 'finished',
                         'finished': clock.time(), 'blocks': number, 'originals': originals}, state_path)


def command_run():
    request = json.loads(REQUEST.read_text())
    specs = catalog(request.get('unit'), request.get('units'))
    names = request.get('names') or [request['name']]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Freezing every app is only acceptable while nobody is using them.
    only_locked = request.get('only_locked', False) or 'freeze-apps' in names
    experiment([specs[name] for name in names], request.get('minutes', 60), only_locked)


def command_restore():
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return
    if state.get('status') in ('running', 'paused'):
        specs = catalog(state.get('unit'), state.get('units'))
        for name, original in (state.get('originals') or {state['name']: state['original']}).items():
            spec = specs.get(name)
            if spec and spec.get() != original:
                spec.set(original)
        write_state(dict(state, status='finished', finished=time.time()))


def command_apply(assignments):
    """Set settings now (`name=value`), e.g. the panel's best combination.
    Runtime only: a reboot, and for ASPM/APST a suspend, restores the defaults."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get('status') in ('running', 'paused'):
        raise SystemExit('an experiment is running; stop it first')
    specs = catalog()
    for assignment in assignments:
        name, _, value = assignment.partition('=')
        if name not in specs or not value:
            raise SystemExit(f'expected NAME=VALUE with NAME one of {", ".join(specs)}, got {assignment!r}')
        spec = specs[name]
        if value in ('saving', 'normal'):
            if spec.levels is None:
                raise SystemExit(f'{name} has no {value} value')
            value = spec.levels()[0 if value == 'saving' else 1]
        if spec.get() != value:
            spec.set(value)


def describe(spec):
    saving = normal = None
    try:
        current = spec.get()
        alternative = spec.alternative(current) if current is not None else None
        error = None if current is not None else 'not available'
        if spec.levels and current is not None:
            saving, normal = spec.levels()
    except Exception as exc:  # noqa: BLE001 - shown to the user, never fatal
        current = alternative = None
        error = str(exc)
    return {'name': spec.name, 'label': spec.label, 'description': spec.description, 'root': spec.root,
            'visible': spec.visible, 'sleep_restored': spec.sleep_restored, 'ac_managed': spec.ac_managed,
            'current': current, 'alternative': alternative, 'saving': saving, 'normal': normal, 'error': error}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='power-experiment', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('units')
    sub.add_parser('status')
    sub.add_parser('enable', help='start automatic experiments (they persist across logins)')
    sub.add_parser('disable', help='stop automatic experiments and restore the settings')
    chosen = sub.add_parser('pool', help='settings automatic experiments may vary')
    chosen.add_argument('names', nargs='*', metavar='name')
    sub.add_parser('auto', help='the power-experiments.service body')
    sub.add_parser('resume', help='restart automatic experiments if enabled (after a manual run)')
    start = sub.add_parser('start', help='randomize one setting, or several together')
    start.add_argument('names', nargs='+', metavar='name')
    start.add_argument('--minutes', type=int, default=60)
    start.add_argument('--unit')
    start.add_argument('--only-locked', action='store_true')
    start.add_argument('--keep', action='append',
                       help='freeze-apps: leave scopes matching this (default: t3code, so agents keep running)')
    sub.add_parser('stop')
    apply = sub.add_parser('apply', help='set settings now, e.g. aspm=powersupersave (until reboot)')
    apply.add_argument('assignments', nargs='+', metavar='name=value')
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
                     uncovered=uncovered(), automatic={'enabled': ENABLED.exists(), 'settings': pool()},
                     resting=qol.resting())
        print(json.dumps(state, indent=1))
    elif args.command == 'test-freeze':
        units = freeze_targets(args.keep if args.keep is not None else ['t3code'])
        print(json.dumps({'seconds': args.seconds, 'units': test_freeze(units, args.seconds),
                          'uncovered': uncovered()}, indent=1))
    elif args.command == 'start':
        units = None
        names = list(dict.fromkeys(args.names))
        if {'freeze', 'freeze-apps'} & set(names) and len(names) > 1:
            parser.error('app-freezing experiments run on their own (they need the screen locked)')
        if names == ['freeze-apps']:
            units = freeze_targets(args.keep if args.keep is not None else ['t3code'])
            if not units:
                parser.error('no app scopes to freeze')
        known = catalog(args.unit, units)
        for name in names:
            if name not in known:
                parser.error(f'unknown experiment {name!r}' + (' (needs --unit)' if name == 'freeze' else ''))
        write_state({'names': names, 'minutes': max(8, min(args.minutes, 24 * 60)), 'unit': args.unit,
                     'units': units, 'only_locked': args.only_locked}, REQUEST)
        run(['systemctl', '--user', 'restart', 'power-experiment.service'])
    elif args.command == 'apply':
        command_apply(args.assignments)
    elif args.command == 'enable':
        SAVED.mkdir(parents=True, exist_ok=True)
        ENABLED.touch()
        run(['systemctl', '--user', 'start', AUTO_UNIT])
    elif args.command == 'disable':
        ENABLED.unlink(missing_ok=True)
        run(['systemctl', '--user', 'stop', AUTO_UNIT], check=False)
    elif args.command == 'pool':
        if args.names:
            eligible = [name for name, spec in catalog().items() if spec.levels]
            unknown = [name for name in args.names if name not in eligible]
            if unknown:
                parser.error(f"not automatic settings: {', '.join(unknown)} (choose from {', '.join(eligible)})")
            write_state({'settings': list(dict.fromkeys(args.names))}, POOL)
        print(' '.join(pool()))
    elif args.command == 'auto':
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        automatic()
    elif args.command == 'resume':
        if ENABLED.exists():
            run(['systemctl', '--user', '--no-block', 'start', AUTO_UNIT], check=False)
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
