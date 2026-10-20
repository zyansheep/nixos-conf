#!/usr/bin/env python3
"""Randomized A/B ("switchback") battery experiments.

A run randomizes one or more settings, each between its stock value (arm A)
and an alternative (arm B), in blocks of BLOCK seconds, only while on
battery. Several settings run as a factorial design (see `design`): every block
informs every setting at once, and settings randomized together reveal whether
they interact. The first WASHOUT seconds of a block are marked so analysis can
skip the battery reading's lag and the system settling. waybar-monitor tags
every record with the running arms (from the state file below); the battery
panel's Experiments tab fits all settings' effects together (report.lever_effects).

    power-experiment list | units | status
    power-experiment enable | disable          (automatic experiments, see `automatic`; disable = stock values)
    power-experiment pool [<name> ...] [--risky]        (the settings they may vary)
    power-experiment pool add|remove <name> ... [--risky]
    power-experiment quarantine | release <name> ... | release all   (settings the watchdog took out)
    power-experiment goal [battery|comfort] [--apply [--risky]]   (what the best combination optimizes)
    power-experiment start <name> [<name> ...] [--minutes N] [--unit SCOPE] [--only-locked] [--risky]
    power-experiment apply <name>=<value> ... [--risky]   (on battery, e.g. the panel's best combination)
    power-experiment start freeze-apps [--keep PATTERN ...]   (always only while locked)
    power-experiment stop | restore [--stock]
    power-experiment reconcile   (power-experiment-reconcile.timer: leftovers, applied settings, AC changes)
    power-experiment next-boot default|psr | boot-status      (boot-level experiments)
    power-experiment test-freeze [--seconds 5] [--keep PATTERN]  (freeze now, measure, thaw)
    power-experiment run | auto   (the power-experiment / power-experiments service bodies)

Stock values are declarative (`Setting.baseline`), never captured from the
live state: the kernel command line's ASPM policy and APST limit, GPU DPM auto,
no CPU cap, Wi-Fi power saving on, no app throttle, power-profiles-daemon's
boost for the profile, ABM by power-conf.nix's udev policy and your own profile
(captured only while nothing of power-experiment's holds it, per power source).

RISKY settings (PCIe ASPM, NVMe APST, GPU DPM) touch the links and the data
fabric that died awake three times in October 2026. They stay out of automatic
experiments, `apply` and `goal --apply` unless asked for with --risky, and when
they do change (automatic, manual and applied alike) they hold for RISKY_HOLD,
never change within RESUME_SETTLE of a resume and change at most RISKY_DAILY
times a day. The watchdog (`Watchdog`) reads the kernel log before every run,
while it runs, and every minute while applied settings exist (the reconciler):
an xHCI death, a Bluetooth command timeout, a PCIe AER error, an amdgpu ring or
NVMe timeout puts every setting back to stock, quarantines the settings changed
in the QUARANTINE_WINDOW before it (and those still off stock), turns automatic
experiments off and notifies. So does a boot that ended with a run active and
a crash reset reason while awake (a sync flood; not a reboot or power-off). A
boot that ended with a run active while asleep (s2idle never came back, or a
sync flood after a suspend entry), or with no shutdown logged (a battery cut, a
hard power-off), only stops the run (until the next login) and notifies: the
s2idle crashes happen with or without experiments, and the settings are at
their boot values after a reboot anyway. Quarantined settings stay out until
`release`.

Settings that cannot change at runtime (PSR: amdgpu.dcdebugmask) are boot-level
experiments: the `psr` specialisation boots with PSR enabled, `next-boot` picks
the entry for the next restart only, and the panel compares boots by arm.

Root settings go through `sudo -n power-lab` (the setuid wrapper); its
pre-sleep hook restores the s2idle crash workarounds (ASPM, NVMe APST, GPU DPM)
before every suspend and refuses changes until its post-resume hook has run.
After resume they stay at stock: the runner and the reconciler set them again
once RESUME_SETTLE has passed, within the hold and the daily limit.
"""
import argparse
import contextlib
import fcntl
import itertools
import json
import math
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
import throttle

BLOCK = 240
WASHOUT = 60
GIVE_UP = 7 * 86400  # A run that cannot collect its battery time in a week ends.
# Automatic experiments: on while ENABLED exists (power-experiments.service's
# condition), varying the settings listed in POOL.
SAVED = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'waybar-monitor'
ENABLED, POOL = SAVED / 'experiments-enabled', SAVED / 'experiments.json'
# Every run and every change power-experiment made (Ledger): the watchdog's and
# the risky settings' memory, across restarts and reboots.
LOG = SAVED / 'experiment-log.json'
# Settings `apply` set, for this boot: reconcile keeps them on battery only.
APPLIED = SAVED / 'applied.json'
AUTO_UNIT = 'power-experiments.service'
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
STATE = RUNTIME / 'experiment.json'
REQUEST = RUNTIME / 'experiment-request.json'
LOCK = RUNTIME / 'experiment.lock'  # Held by whoever changes settings (see `ownership`).
SOURCE = RUNTIME / 'power-source.json'  # reconcile's view of AC/battery, and when it last changed.
# The setuid wrapper: Waybar's PATH has only the system profile, whose sudo is not setuid.
POWER_LAB = ['/run/wrappers/bin/sudo', '-n', '/run/current-system/sw/bin/power-lab']
BUSY_EXIT = 75  # power-lab: suspending (its sleep-safe holds), or another change holds its lock.
SYS = Path('/sys')
CMDLINE = Path('/proc/cmdline')

# Settings on the PCIe links, the SSD's power states and the data fabric clocks.
RISKY = ('aspm', 'apst', 'gpu-dpm')
RISKY_HOLD = 45 * 60  # s a risky setting keeps a value once changed.
RISKY_DAILY = 8  # Changes of one risky setting a day (both ways; going back to stock is never refused).
RESUME_SETTLE = 60  # s after resume without any change (the hooks and the Wi-Fi module settle).
QUARANTINE_WINDOW = 15 * 60  # s before an incident whose changes the watchdog quarantines.
WATCH_EVERY = 30  # s between kernel-log checks while running.
BACKOFF, BACKOFF_MAX, FAILURES_MAX = 60, 3600, 4  # A failing setting: 60 s, 120 s, … then out for the run.
RECHECK = 6 * 3600  # s between looks at whether testing is worth it again, while it isn't.
RETEST = 10 * 3600  # s of blocks after a kernel (major.minor) or BIOS change before testing may stop.
SETTLED = 'more testing would cost more than it saves'  # The runner's pause while testing isn't worth it.
# s for a power-lab call: past its 10 s wait for another change's lock (it then
# exits BUSY_EXIT) plus the change itself, so a busy lock reads as Busy, not as
# a timeout that leaves power-lab applying the change after we gave up.
LAB_TIMEOUT = 30
# s for `systemctl stop` of a run: its stop restores everything (TimeoutStopSec=180 in niri.nix).
STOP_TIMEOUT = 240


class Busy(RuntimeError):
    """power-lab refused for now (suspending, or its lock is held): wait, don't count it as a failure."""


def run(command, check=True, timeout=15):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        error = Busy if result.returncode == BUSY_EXIT else RuntimeError
        raise error(f"{' '.join(command)}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def lab(*args):
    """`sudo -n power-lab <args>`."""
    return run(POWER_LAB + list(args), timeout=LAB_TIMEOUT)


def systemctl_wait(*args, check=False):
    """`systemctl --user <args>` that may stop a run (stop, restart, a start with
    Conflicts=): waits for its restore, and never dies of a slow one."""
    try:
        return run(['systemctl', '--user', *args], check=check, timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"power-experiment: systemctl {' '.join(args)} is still running after {STOP_TIMEOUT} s", file=sys.stderr)
        return ''


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def boot_id():
    return (read('/proc/sys/kernel/random/boot_id') or '').replace('-', '')


def cmdline_param(name):
    """`name`'s value on the kernel command line (the last one wins, as in the kernel), or None."""
    values = [word.partition('=')[2] for word in (read(CMDLINE) or '').split() if word.startswith(name + '=')]
    return values[-1] if values else None


def notify(summary, body):
    try:
        subprocess.run(['notify-send', '-u', 'critical', '-a', 'power-experiment', summary, body],
                       capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


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
                 sleep_restored=False, unit=None, ac_managed=False, units=None, levels=None, baseline=None,
                 reset=None):
        self.name, self.label, self.description = name, label, description
        self.get, self.set, self.alternative = get, set, alternative
        # () → (power-saving value, normal value), for `apply name=saving|normal`.
        self.levels = levels
        self.root, self.visible, self.sleep_restored, self.unit = root, visible, sleep_restored, unit
        # udev switches these on AC/battery changes; never fight it while plugged in.
        self.ac_managed = ac_managed
        self.units = units
        # (on_battery) → the stock value restores go back to: declarative, never the live value.
        self.baseline = baseline
        # () → None: puts the stock value back another way than set() (ABM: the udev policy).
        self.reset = reset

    def read(self):
        """The current value, or None while the setting is unavailable (no Wi-Fi
        interface, power-profiles-daemon restarting)."""
        try:
            return self.get()
        except Exception:  # noqa: BLE001 - unavailable is a state, not a crash
            return None

    def stock(self, on_battery=True):
        if self.baseline is None:
            raise RuntimeError('no stock value')
        return self.baseline(on_battery)

    def stock_or_none(self, on_battery=True):
        try:
            return self.stock(on_battery)
        except Exception:  # noqa: BLE001 - e.g. boost while power-profiles-daemon is down
            return None


CPUFREQ = SYS / 'devices/system/cpu/cpufreq'


def boost_get():
    """'1' while CPU boost is on: globally and on every CPU. power-profiles-daemon
    sets it per CPU on each profile change (off for power-saver) and leaves the
    global switch on, so the global switch alone says on while it is off."""
    overall = read(CPUFREQ / 'boost')
    if overall is None:
        return None
    return '1' if overall == '1' and all(read(p) in (None, '1') for p in CPUFREQ.glob('policy*/boost')) else '0'


def profile_get():
    return run(['powerprofilesctl', 'get'])


def profile_set(value):
    """powerprofilesctl set, keeping CPU boost as it was: the profile is the
    platform profile and EPP, and boost is a setting of its own."""
    boost = boost_get()
    if read(CPUFREQ / 'boost') == '0' and boost is not None:
        lab('boost', boost)  # The daemon's per-CPU writes fail while boost is off globally.
    run(['powerprofilesctl', 'set', value])
    if boost is not None and boost_get() != boost:
        lab('boost', boost)


PROFILES = ('power-saver', 'balanced', 'performance')
DEFAULT_PROFILE = {'battery': 'power-saver', 'ac': 'balanced'}  # niri.nix's udev rules.


def own_profile(on_battery):
    """Your profile for this power source: captured (capture_profile) only while
    nothing of power-experiment's holds it, else what udev sets."""
    key = 'battery' if on_battery else 'ac'
    saved = preferences().get('own_profile')
    value = saved.get(key) if isinstance(saved, dict) else None
    return value if value in PROFILES else DEFAULT_PROFILE[key]


def boost_stock(_on_battery=True):
    """power-profiles-daemon's boost for the profile now (restored before boost): off under power-saver."""
    return '0' if profile_get() == 'power-saver' else '1'


# power-conf.nix's udev ABM policy (panel_power_savings), by power source.
ABM_POLICY = {True: '3', False: '0'}


def abm_get():
    return read(next(iter(sorted(SYS.glob('class/drm/card*-eDP-*/amdgpu/panel_power_savings'))), '/nonexistent'))


def abm_reset():
    """ABM back to the udev policy by running it again. The trigger runs niri.nix's
    profile rule too, so the profile (and boost, which follows it) are put back
    as they were; restore_settings restores those after ABM when they are its own."""
    try:
        profile = profile_get()
    except Exception:  # noqa: BLE001 - power-profiles-daemon down: nothing to put back
        profile = None
    boost = boost_get()
    lab('trigger', 'power_supply')
    try:
        if profile in PROFILES and profile_get() != profile:
            profile_set(profile)
    finally:
        if boost is not None and boost_get() != boost:
            lab('boost', boost)


def gpu_dpm_get():
    return next((read(path) for path in sorted(SYS.glob('class/drm/card*/device/power_dpm_force_performance_level'))),
                None)


THROTTLE_UNIT = 'app-throttle.service'


def throttle_get():
    return 'on' if run(['systemctl', '--user', 'is-active', THROTTLE_UNIT], check=False) == 'active' else 'off'


CPU_CAP = '2000'  # MHz: the CPU cap's power-saving value (base is 3501, boost 4972).
APST_DEFAULT = '100000'  # µs: nvme_core.default_ps_max_latency_us's default.


def cpu_cap_get():
    """The CPU frequency cap in MHz, or 'none' while the cap is the CPU's own
    maximum (cpuinfo_max_freq, which boost raises)."""
    policy = CPUFREQ / 'policy0'
    cap, top = read(policy / 'scaling_max_freq'), read(policy / 'cpuinfo_max_freq')
    if cap is None or top is None:
        return None
    return 'none' if int(cap) >= int(top) - 50_000 else str(round(int(cap) / 100_000) * 100)


def catalog(unit=None, units=None):
    """Experiment specs; `unit` / `units` parameterize the app-freeze experiments."""
    wifi = wifi_interface()
    specs = [
        Setting('aspm', 'PCIe link power saving (ASPM)',
                'Lets PCIe links (NVMe, Wi-Fi, GPU) sleep. Disabled at boot as an s2idle crash workaround; '
                'restored for every suspend (set again a minute after resume). Risky: only with --risky.',
                aspm_get, lambda v: lab('aspm', v),
                lambda cur: 'powersupersave' if cur != 'powersupersave' else 'performance',
                root=True, sleep_restored=True, levels=lambda: ('powersupersave', 'performance'),
                baseline=lambda on: cmdline_param('pcie_aspm.policy') or 'default'),
        Setting('apst', 'NVMe autonomous power states (APST)',
                'Lets the SSD drop into low-power states when idle. Disabled at boot as an s2idle crash '
                'workaround; restored for every suspend (set again a minute after resume). Risky: only with --risky.',
                lambda: read(apst_paths()[0]) if apst_paths() else None,
                lambda v: lab('apst', v),
                lambda cur: '100000' if cur == '0' else '0', root=True, sleep_restored=True,
                levels=lambda: ('100000', '0'),
                baseline=lambda on: cmdline_param('nvme_core.default_ps_max_latency_us') or APST_DEFAULT),
        Setting('boost', 'CPU boost', 'Clocks above base frequency for short bursts.',
                boost_get, lambda v: lab('boost', v),
                lambda cur: '0' if cur == '1' else '1', root=True, visible='Bursty work gets slower.',
                levels=lambda: ('0', '1'), baseline=boost_stock),
        Setting('abm', 'Panel adaptive backlight (ABM)',
                'Dims the backlight and boosts pixel values to compensate.',
                abm_get, lambda v: lab('abm', v),
                lambda cur: '0' if cur not in (None, '0') else '3', root=True, visible='Slight contrast change.',
                ac_managed=True, levels=lambda: ('3', '0'), baseline=lambda on: ABM_POLICY[bool(on)],
                reset=abm_reset),
        Setting('profile', 'Power profile', 'power-profiles-daemon profile (platform profile and EPP).',
                profile_get, profile_set,
                lambda cur: 'balanced' if cur == 'power-saver' else 'power-saver',
                visible='Responsiveness changes.', ac_managed=True, levels=lambda: ('power-saver', 'balanced'),
                baseline=own_profile),
        Setting('gpu-dpm', 'GPU and memory clocks', 'amdgpu DPM low: pins the GPU, memory and fabric clocks at '
                'their lowest (the fabric otherwise idles at 1600 MHz). Risky: only with --risky.',
                gpu_dpm_get, lambda v: lab('gpudpm', v),
                lambda cur: 'low' if cur != 'low' else 'auto', root=True,
                visible='Video, WebGL and memory-heavy work get slower.', levels=lambda: ('low', 'auto'),
                baseline=lambda on: 'auto'),
        Setting('cpu-cap', 'CPU frequency cap', f'Caps every core at {CPU_CAP} MHz (base 3501, boost 4972).',
                cpu_cap_get, lambda v: lab('cpucap', v),
                lambda cur: CPU_CAP if cur == 'none' else 'none', root=True,
                visible='Bursty work gets slower.', levels=lambda: (CPU_CAP, 'none'), baseline=lambda on: 'none'),
        Setting('throttle-apps', 'Throttle background apps',
                f'Caps every app but the focused one at {throttle.QUOTA}% of a core while on battery (app-throttle); '
                'apps playing or recording audio or video, terminals, editors, T3 Code and the input method are '
                'left alone.',
                throttle_get, lambda v: run(['systemctl', '--user', 'start' if v == 'on' else 'stop', THROTTLE_UNIT]),
                lambda cur: 'off' if cur == 'on' else 'on',
                visible='Background apps update slowly until you switch to them.', levels=lambda: ('on', 'off'),
                baseline=lambda on: 'off'),
        Setting('refresh', 'Display refresh rate', 'Lower internal-panel refresh rate.',
                refresh_get, lambda v: run(['niri', 'msg', 'output', niri_internal()['name'], 'mode', v]),
                refresh_alternative, visible='Motion looks slightly less smooth.', levels=refresh_levels,
                baseline=lambda on: refresh_levels()[1]),
    ]
    if wifi:
        specs.append(Setting(
            'wifi-ps', 'Wi-Fi power saving', 'Radio sleeps between beacons (adds latency).',
            lambda: run(['iw', 'dev', wifi, 'get', 'power_save']).split(':')[-1].strip(),  # Raises while gone.
            lambda v: lab('wifips', v),
            lambda cur: 'off' if cur == 'on' else 'on', root=True, visible='Network latency changes.',
            levels=lambda: ('on', 'off'), baseline=lambda on: 'on'))
    if unit:
        specs.append(Setting(
            'freeze', f'Close {unit_label(unit)}', 'Freezes the app (cgroup freezer) to measure its cost.',
            lambda: freeze_get(unit),
            lambda v: run(['systemctl', '--user', 'freeze' if v == 'frozen' else 'thaw', unit]),
            lambda cur: 'frozen', visible='The app stops responding during B blocks.', unit=unit,
            baseline=lambda on: 'running'))
    if units:
        specs.append(Setting(
            'freeze-apps', f'Close every app ({len(units)} scopes)',
            'Freezes all app scopes while the screen is locked: frozen blocks measure the platform floor, '
            'and the difference is what the apps cost in the background.',
            lambda: freeze_all_get(units), lambda v: freeze_all_set(units, v), lambda cur: 'frozen',
            visible='Only runs while the screen is locked.', units=units, baseline=lambda on: 'running'))
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


def load_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def flocked(path, wait=0.0):
    """An exclusive flock on `path`; yields whether it was had within `wait` seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ownership(wait=0.0):
    """The settings' owner lock: a runner holds it for as long as it runs, and
    `apply`, `restore`, `reconcile` and `disable` take it, so nothing changes a
    setting under a run (or under another of them)."""
    return flocked(LOCK, wait)


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


# --- What power-experiment did: the ledger -----------------------------------------------

class Ledger:
    """LOG: the runs (sessions, with a heartbeat) and every change power-experiment
    made, for KEEP seconds. The watchdog asks it whether a run was active when
    something broke and what had changed; the risky settings' hold and daily
    limit count its changes; reconcile finds what a crashed run left behind."""
    KEEP = 3 * 86400
    BEAT = 60

    def __init__(self, path=None, clock=time.time):
        self.path, self.clock = path or LOG, clock
        self.session, self.beaten = None, 0.0

    def load(self):
        data = load_json(self.path)
        for key in ('sessions', 'changes'):
            if not isinstance(data.get(key), list):
                data[key] = []
        return data

    def save(self, data):
        cutoff = self.clock() - self.KEEP
        data['sessions'] = [s for s in data['sessions'] if (s.get('end') or s.get('last') or 0) >= cutoff][-200:]
        data['changes'] = [c for c in data['changes'] if c.get('t', 0) >= cutoff][-5000:]
        write_state(data, self.path)

    def start(self, mode):
        now = self.clock()
        data = self.load()
        self.session = {'boot': boot_id(), 'mode': mode, 'pid': os.getpid(), 'start': now, 'last': now, 'end': None}
        data['sessions'].append(dict(self.session))
        self.save(data)
        self.beaten = now

    def update(self, **fields):
        data = self.load()
        for session in data['sessions']:
            if (session.get('start'), session.get('pid')) == (self.session['start'], self.session['pid']):
                session.update(fields)
        self.session.update(fields)
        self.save(data)

    def beat(self):
        now = self.clock()
        if self.session and now - self.beaten >= self.BEAT:
            self.update(last=now)
            self.beaten = now

    def end(self):
        if self.session:
            now = self.clock()
            self.update(last=now, end=now)
            self.session = None

    def change(self, name, value, stock, by):
        data = self.load()
        data['changes'].append({'t': self.clock(), 'boot': boot_id(), 'name': name, 'value': value,
                                'stock': stock, 'by': by})
        self.save(data)

    def changes(self, name=None):
        return sorted((c for c in self.load()['changes'] if name is None or c.get('name') == name),
                      key=lambda c: c.get('t', 0))

    def last_change(self, name):
        changes = self.changes(name)
        return changes[-1]['t'] if changes else None

    def count(self, name, since):
        return sum(c['t'] >= since for c in self.changes(name))

    def last_values(self, boot, before=math.inf):
        """{name: its last change} in boot `boot` up to `before`."""
        return {c['name']: c for c in self.changes() if c.get('boot') == boot and c['t'] <= before}

    def suspects(self, t, boot, window=QUARANTINE_WINDOW):
        """Settings changed in the `window` before `t`, and those still off stock at `t`."""
        changed = {c['name'] for c in self.changes() if c.get('boot') == boot and t - window <= c['t'] <= t}
        held = {name for name, c in self.last_values(boot, t).items() if c.get('stock') is not True}
        return changed | held

    def active(self, t, boot, slack=120):
        """Whether a run was active at `t` in boot `boot` (up to `slack` after its last heartbeat)."""
        return any(s.get('boot') == boot and s.get('start', math.inf) <= t
                   <= (s.get('end') or s.get('last') or s.get('start', 0)) + slack
                   for s in self.load()['sessions'])

    def failing(self, name):
        """(failed restores in a row, time of the last) for `name`."""
        count, last = 0, None
        for c in reversed(self.changes(name)):
            if not str(c.get('by', '')).endswith('(failed)'):
                break
            count, last = count + 1, last or c['t']
        return count, last

    def due(self, name, now):
        """Whether a restore of `name` may be tried again: BACKOFF after a failed one, doubling."""
        count, last = self.failing(name)
        return not count or now >= last + min(BACKOFF_MAX, BACKOFF * 2 ** (count - 1))

    def unclean(self, boot):
        """The last run of boot `boot` that never ended: it was active when that boot ended."""
        found = [s for s in self.load()['sessions'] if s.get('boot') == boot and not s.get('end')]
        return max(found, key=lambda s: s.get('last', 0)) if found else None


def leftovers(ledger, exclude=()):
    """Settings power-experiment last left off stock in this boot (a crashed run's)."""
    return [name for name, c in ledger.last_values(boot_id()).items()
            if c.get('stock') is not True and name not in exclude]


# --- The watchdog ------------------------------------------------------------------------

# Kernel messages that stop every experiment: how the awake failures of October
# 2026 looked (a Bluetooth command timeout came ~5 s before each xHCI death).
INCIDENTS = (
    ('xHCI controller died', re.compile(r'xhci_hcd .*HC died')),
    ('Bluetooth command timeout', re.compile(r'Bluetooth: hci\d+: command (?:0x[0-9a-f]+ )?tx timeout')),
    ('PCIe AER error', re.compile(r'AER: (?:Corrected|Uncorrected|Multiple)|PCIe Bus Error')),
    ('amdgpu ring timeout', re.compile(r'amdgpu.*ring \S+ timeout')),
    ('NVMe timeout', re.compile(r'nvme nvme\d+: .*(?:timeout|controller is down)')),
)
# How the boot before this one ended (x86/amd, printed early in this boot): one
# line per reason bit. Bit 11 carries no text and is always set here; the CF9
# resets (reboot, also a panic or SysRq reboot) and the ACPI power state
# transition (power-off) are clean. Anything else (bit 27: a data fabric sync
# flood; a 4 s power button press, a thermal trip, a watchdog …) is a crash.
RESET_REASON = re.compile(r'Previous system reset reason \[(0x[0-9a-fA-F]+)\]')
CLEAN_RESETS = 0x800 | 0x40000 | 0x80000 | 0x100000 | 0x200000
SYNC_FLOOD_BIT = 0x8000000
SUSPEND_MARK = re.compile(r'PM: suspend (entry|exit)')
# journalctl -g's prefilter (PCRE2); INCIDENTS, RESET_REASON and SUSPEND_MARK decide.
JOURNAL_GREP = (r'HC died|tx timeout|AER: |PCIe Bus Error|ring \S+ timeout|nvme nvme|'
                r'Previous system reset reason|PM: suspend (?:entry|exit)')


class Journal:
    """The kernel log through journalctl (users can't read /dev/kmsg here: dmesg_restrict)."""

    @staticmethod
    def entries(command):
        try:
            result = subprocess.run(['journalctl', '--no-pager', '-q', '-o', 'json'] + command,
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode not in (0, 1):  # 1: nothing matched (or no such boot).
            return None
        entries = []
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                message = entry.get('MESSAGE', '')
                if isinstance(message, list):  # Not UTF-8: journalctl gives the bytes.
                    message = bytes(message).decode(errors='replace')
                entries.append({'t': int(entry['__REALTIME_TIMESTAMP']) / 1e6, 'boot': entry.get('_BOOT_ID', ''),
                                'message': str(message)})
            except (ValueError, KeyError, TypeError):
                continue
        return entries

    def kernel(self, boot='0', since=None):
        """Candidate kernel messages of boot `boot` (0 this one, -1 the one before), or None if unreadable."""
        command = ['-k', '-b', str(boot), '-g', JOURNAL_GREP]
        return self.entries(command + (['--since', f'@{int(since)}'] if since else []))

    def readable(self):
        """Whether this user can read the kernel log at all (without access journalctl just shows nothing)."""
        return bool(self.entries(['-k', '-b', '0', '-n', '1']))

    def previous_boot(self):
        """(id, time of its last entry) of the boot before this one, or None."""
        entries = self.entries(['-b', '-1', '-n', '1'])
        return (entries[-1]['boot'], entries[-1]['t']) if entries else None

    def shutdown_logged(self, boot='-1'):
        """Whether boot `boot` shut down or rebooted cleanly (logind logged it), None if unreadable."""
        entries = self.entries(['-b', str(boot), '-u', 'systemd-logind.service',
                                '-g', 'System is (?:powering down|rebooting)', '-n', '1'])
        return None if entries is None else bool(entries)

    def last_resume(self):
        """When this boot last resumed from suspend, or None."""
        entries = self.entries(['-k', '-b', '0', '-g', 'PM: suspend exit', '-n', '1'])
        return max((e['t'] for e in entries), default=None) if entries else None


def classify(entry):
    return next((what for what, pattern in INCIDENTS if pattern.search(entry['message'])), None)


def ending(current, previous, shutdown_logged):
    """How the boot before this one ended, from this boot's reset reason and that
    boot's kernel log: (what, message, quarantine), or None for a clean shutdown.
    `shutdown_logged` () → whether that boot logged a clean shutdown (None: unknown)."""
    resets = [(int(m.group(1), 16), e['message']) for e in current if (m := RESET_REASON.search(e['message']))]
    marks = sorted((e['t'], m.group(1)) for e in previous if (m := SUSPEND_MARK.search(e['message'])))
    asleep = bool(marks) and marks[-1][1] == 'entry'  # It suspended and never resumed.
    crashes = [(value, message) for value, message in resets if value & ~CLEAN_RESETS]
    if crashes:
        value, message = crashes[0]
        what = 'data fabric sync flood' if value & SYNC_FLOOD_BIT else f'system reset {value:#010x}'
        if asleep:  # The s2idle crashes happen with or without experiments: stop, quarantine nothing.
            return f'{what} while asleep (s2idle)', message, False
        return f'{what} (the system crashed)', message, True
    if asleep:
        return 'the system died asleep (s2idle never resumed)', 'PM: suspend entry with no exit', False
    if shutdown_logged() is not True:
        return ('the last boot ended without a shutdown (a crash asleep or awake, a battery cut or a hard power-off)',
                resets[0][1] if resets else 'no reset reason, no shutdown logged', False)
    return None


class Watchdog:
    """Incidents (INCIDENTS, or a boot that ended badly) that power-experiment may
    have caused: while one of its runs was active, after `seen` (the last
    incident handled, or the last explicit `enable`). An incident whose
    'quarantine' is False stops experiments but quarantines nothing."""

    def __init__(self, journal=None, ledger=None):
        self.journal = journal or Journal()
        self.ledger = ledger or Ledger()

    def preflight(self, seen):
        """Before a run: the previous boot, and this one so far. None while the kernel log is unreadable."""
        if not self.journal.readable():
            return None
        found = []
        current = self.journal.kernel('0')
        previous = self.journal.kernel('-1') or []
        if current is None:
            return None
        last = self.journal.previous_boot()
        session = self.ledger.unclean(last[0]) if last else None
        if session:  # The last boot ended with a run active: how did it end?
            at = max(last[1], session.get('last', 0))
            ended = ending(current, previous, self.journal.shutdown_logged) if at > seen else None
            if ended:
                what, message, quarantine = ended
                found.append({'t': at, 'boot': last[0], 'what': what, 'message': message, 'quarantine': quarantine})
        for entry in previous + current:
            what = classify(entry)
            if what and entry['t'] > seen and self.ledger.active(entry['t'], entry['boot']):
                found.append(dict(entry, what=what))
        return found

    def poll(self, since, seen):
        """While running: this boot's incidents since `since`."""
        entries = self.journal.kernel('0', since) or []
        return [dict(e, what=w) for e in entries if (w := classify(e)) and e['t'] > seen]


def seen():
    try:
        return float(preferences().get('watchdog_seen') or 0)
    except (TypeError, ValueError):
        return 0.0


def stock_names(specs, ledger):
    """What `disable` and the watchdog put back: every power setting, and the refresh
    rate (a display preference) only while a change of power-experiment's holds it."""
    ours = set(leftovers(ledger))
    return [name for name, spec in specs.items() if spec.baseline is not None and (name != 'refresh' or name in ours)]


def trip(found, specs, battery, ledger, restore=True):
    """The incident brake: every setting back to stock and a notification; the run
    that trips it stops. For an incident that may be a setting's doing (the
    kernel's, or a crash reset while awake) also: the suspects (changed in the
    QUARANTINE_WINDOW before it, or off stock at it) quarantined until `release`,
    applied settings dropped and automatic experiments off. One that isn't (a
    boot that died asleep or without a shutdown: 'quarantine' False) leaves them
    on: they start again at the next login (or `power-experiment resume`)."""
    now = time.time()
    reason = '; '.join(dict.fromkeys(incident['what'] for incident in found))
    attributable = any(incident.get('quarantine', True) for incident in found)
    suspects = set()
    for incident in found:
        if incident.get('quarantine', True):
            suspects |= ledger.suspects(incident['t'], incident['boot'])
    pending = restore_settings(specs, stock_names(specs, ledger), battery, ledger, by='watchdog') if restore else {}
    if attributable:
        APPLIED.unlink(missing_ok=True)
        ENABLED.unlink(missing_ok=True)
    first = min(found, key=lambda incident: incident['t'])
    record = {'at': now, 'reason': reason, 'event': first['t'], 'message': first['message'][:300],
              'quarantined': sorted(suspects), 'pending': pending, 'disabled': attributable}

    def update(prefs):
        quarantine = dict(prefs.get('quarantine') or {})
        for name in sorted(suspects):
            quarantine.setdefault(name, {'reason': reason, 'since': now, 'event': first['t'],
                                         'message': first['message'][:300]})
        prefs.update(quarantine=quarantine, last_trip=record,
                     watchdog_seen=max([seen()] + [incident['t'] for incident in found]))
    update_preferences(update)
    names = ', '.join(sorted(suspects)) or ('nothing changed recently' if attributable
                                            else 'nothing (not a sign of any setting)')
    notify('Power experiments stopped', f"{reason} at {time.strftime('%H:%M', time.localtime(first['t']))}. "
           f'Settings are back to stock; quarantined: {names}. '
           + ('Automatic experiments are off.' if attributable else
              'Automatic experiments start again at your next login.'))
    print(f'power-experiment: watchdog: {reason} ({first["message"][:200]}); quarantined {names}'
          + (f'; could not restore {", ".join(pending)}' if pending else ''), file=sys.stderr)
    return record


# --- Putting settings back -----------------------------------------------------------------

# Restores go risky first (back to safety soonest), ABM before the profile (its
# udev trigger runs the profile rule) and the profile before boost (which follows it).
RESTORE_ORDER = ('aspm', 'apst', 'gpu-dpm', 'abm', 'profile', 'boost', 'cpu-cap', 'wifi-ps', 'throttle-apps',
                 'refresh', 'freeze', 'freeze-apps')


def restore_rank(name):
    return RESTORE_ORDER.index(name) if name in RESTORE_ORDER else len(RESTORE_ORDER)


def risky_last(names):
    return sorted(names, key=lambda name: name in RISKY)


READBACK = (8, 0.25)  # Tries and seconds between them: niri applies a mode change a moment later.


def settled(spec, value):
    """`spec`'s value once it reads `value` (or after READBACK runs out)."""
    tries, pause = READBACK
    for attempt in range(tries):
        if (now := spec.read()) == value or attempt == tries - 1:
            return now
        time.sleep(pause)


def change(spec, value, ledger, by):
    """Log, set `spec` to `value` and read it back. The attempt is logged first: one
    that fails half-way still counts (risky limits, suspects, leftovers)."""
    if ledger:
        ledger.change(spec.name, value, value == spec.stock_or_none(True), by)
    spec.set(value)
    if (now := settled(spec, value)) != value:
        raise RuntimeError(f'reads {now!r} after setting {value!r}')


def restore_settings(specs, names, battery, ledger=None, by='restore', captured=None, patient=False):
    """Put `names` back to their stock values, each on its own, reading every one
    back. {name: why} for those not verified (restore-pending; retried later).
    `patient` (retries: the runner's pauses, the reconciler) skips a setting whose
    last restores failed until its backoff has passed, so a broken one is not
    written every few seconds."""
    pending, on = {}, battery()
    for name in sorted(dict.fromkeys(names), key=restore_rank):
        spec = specs.get(name)
        if spec is None:
            pending[name] = 'unavailable'
            continue
        if patient and ledger and not ledger.due(name, ledger.clock()):
            pending[name] = 'backing off after failed restores'
            continue
        if spec.ac_managed and not on:
            continue  # udev applied the AC value.
        attempted, target = False, None
        try:
            target = spec.stock(on) if spec.baseline is not None else (captured or {}).get(name)
            if target is None:
                raise RuntimeError('no stock value')
            now = spec.read()
            if now != target:
                if now is None:
                    raise RuntimeError('unavailable')
                attempted = True
                spec.reset() if spec.reset else spec.set(target)
                now = settled(spec, target)
            if now != target:
                raise RuntimeError(f'reads {now!r}, not {target!r}')
            if attempted and ledger:
                ledger.change(name, target, True, by)
        except Exception as error:  # noqa: BLE001 - restore the rest; the reconciler retries
            pending[name] = str(error) or type(error).__name__
            print(f'power-experiment: could not restore {name}: {pending[name]}', file=sys.stderr)
            if attempted and ledger:  # Not stock: a leftover until a restore succeeds.
                ledger.change(name, target, None, by + ' (failed)')
    return pending


def risky_allowed(name, current, value, ledger, now, stock, hold=True):
    """May risky setting `name` go from `current` to `value` now? Back to stock: after
    the hold. Off stock: after the hold and with two of the day's changes left
    (one to come back)."""
    if value == current:
        return True
    last = ledger.last_change(name)
    if hold and last is not None and now - last < RISKY_HOLD:
        return False
    return value == stock or ledger.count(name, now - 86400) + 2 <= RISKY_DAILY


def finished(base, pending, **fields):
    return dict(base, status='restore-pending' if pending else 'finished', pending=sorted(pending),
                restore_errors=pending, **fields)


# --- Runs ------------------------------------------------------------------------------------

def experiment(specs, minutes, only_locked=False, block=BLOCK, washout=WASHOUT, clock=Clock,
               battery=on_battery, locked=screen_locked, state_path=None, rng=None, watchdog=None, ledger=None):
    """Randomize `specs` (one Setting or several) together in blocks until `minutes`
    of battery time are collected (AC and, with `only_locked`, unlocked time don't count).
    Arm A is each setting's stock value; the run ends with all of them back at it."""
    specs = [specs] if isinstance(specs, Setting) else list(specs)
    by_name = {spec.name: spec for spec in specs}
    names = [spec.name for spec in specs]
    ledger = ledger or Ledger(clock=clock.time)
    watchdog = watchdog or Watchdog(ledger=ledger)
    started = clock.time()
    stock = {}
    for spec in specs:
        stock[spec.name] = spec.stock_or_none(True) if spec.baseline is not None else spec.read()
        if stock[spec.name] is None:
            raise RuntimeError(f'{spec.name}: setting is not available here')
    arms = {spec.name: {'A': stock[spec.name], 'B': spec.alternative(stock[spec.name])} for spec in specs}
    # `name`, `arm` and `value` are what waybar-monitor tags records with:
    # 'aspm+apst', 'BA' and {'aspm': …, 'apst': …} (a single setting: 'B' and its value).
    base = {'run': f"{'+'.join(names)}-{int(started)}", 'name': '+'.join(names), 'names': names,
            'label': ' + '.join(spec.label for spec in specs), 'arms': arms, 'originals': stock, 'baseline': stock,
            'unit': specs[0].unit, 'units': specs[0].units, 'started': started, 'target_seconds': minutes * 60,
            'gives_up_at': started + GIVE_UP, 'block_seconds': block, 'washout_seconds': washout}
    found = watchdog.preflight(seen())
    if found is None:
        reason = 'the watchdog cannot read the kernel log (journalctl -k); not starting'
        write_state(finished(base, {}, reason=reason, finished=clock.time(), blocks=0, collected=0), state_path)
        raise RuntimeError(reason)
    if found:
        record = trip(found, by_name, battery, ledger)
        write_state(finished(base, record['pending'], watchdog=record, finished=clock.time(), blocks=0, collected=0),
                    state_path)
        return
    configs = design(names, rng or random.Random())
    number, collected, resumed, watched, tripped = 0, 0.0, None, clock.time(), None
    ledger.start('manual')

    def restore(patient=False):
        return restore_settings(by_name, names, battery, ledger, by='manual', captured=stock, patient=patient)
    try:
        _, mark = clock.suspended_since(None)
        while collected < base['target_seconds'] and clock.time() < base['gives_up_at']:
            ledger.beat()
            suspended, mark = clock.suspended_since(mark)
            resumed = clock.time() if suspended else resumed
            if clock.time() - watched >= WATCH_EVERY:
                found, watched = watchdog.poll(watched - 10, seen()), clock.time()
                if found:
                    tripped = trip(found, by_name, battery, ledger)
                    break
            if resumed is not None and clock.time() - resumed < RESUME_SETTLE:
                # Nothing changes right after resume (the hooks put things back; Wi-Fi reloads).
                write_state(dict(base, status='paused', reason='settling after resume', blocks=number,
                                 collected=collected), state_path)
                clock.sleep(5)
                continue
            reason = ('on AC power' if not battery() else
                      'waiting for the screen to lock' if only_locked and not locked() else None)
            if reason:
                restore(patient=True)  # Every 15 s: one that keeps failing waits out its backoff.
                write_state(dict(base, status='paused', reason=reason, blocks=number, collected=collected), state_path)
                clock.sleep(15)
                continue
            config = dict(next(configs))
            values = {name: arms[name][config[name]] for name in names}
            held = []
            try:
                now = clock.time()
                for spec in sorted(specs, key=lambda spec: spec.name in RISKY):  # Risky settings last.
                    present = spec.get()
                    if spec.name in RISKY and not risky_allowed(spec.name, present, values[spec.name], ledger, now,
                                                                stock[spec.name]):
                        # The hold and the daily limit, however often blocks are cut short
                        # (AC, suspend): the block runs with what is there, tagged as such.
                        values[spec.name] = present
                        config[spec.name] = 'A' if present == stock[spec.name] else 'B'
                        held.append(spec.name)
                    elif present != values[spec.name]:
                        change(spec, values[spec.name], ledger, 'manual')
            except Busy as error:
                write_state(dict(base, status='paused', reason=f'power-lab busy ({error})', blocks=number,
                                 collected=collected), state_path)
                clock.sleep(15)
                continue
            begin = clock.time()
            number += 1
            state = dict(base, status='running', block=number, config=config,
                         arm=''.join(config[name] for name in names),
                         value=values[names[0]] if len(names) == 1 else values, block_start=begin,
                         washout_until=begin + washout, block_end=begin + min(block, base['target_seconds'] - collected),
                         blocks=number, collected=collected, held=held)
            write_state(state, state_path)
            found = None
            while clock.time() < state['block_end']:
                clock.sleep(5)
                ledger.beat()
                suspended, mark = clock.suspended_since(mark)
                resumed = clock.time() if suspended else resumed
                if clock.time() - watched >= WATCH_EVERY:
                    found, watched = watchdog.poll(watched - 10, seen()), clock.time()
                interrupted = (found or suspended or not battery() or (only_locked and not locked())
                               or any(spec.get() != values[spec.name] for spec in specs))
                if interrupted:
                    break
            collected += clock.time() - begin
            if clock.time() < state['block_end']:
                write_state(dict(state, block_end=clock.time(), collected=collected), state_path)  # Ended early.
            if found:
                tripped = trip(found, by_name, battery, ledger)
                break
    finally:
        pending = tripped['pending'] if tripped else {}
        try:
            if not tripped:
                pending = restore()
        finally:
            ledger.end()
            write_state(finished(base, pending, finished=clock.time(), blocks=number, collected=collected,
                                 watchdog=tripped), state_path)


GOALS = ('battery', 'comfort')  # What the panel's best combination optimizes.
RECOMMEND = RUNTIME / 'recommend.json'  # Each goal's best combination, from battery-eta's latest fit.


def preferences():
    return load_json(POOL)


def update_preferences(update):
    """Read-modify-write POOL under its lock (the runner and the CLI both write it)."""
    with flocked(POOL.with_suffix('.lock'), wait=10):
        prefs = preferences()
        update(prefs)
        write_state(prefs, POOL)


def prefer(**changes):
    update_preferences(lambda prefs: prefs.update(changes))


def quarantined(prefs=None):
    """{name: why} the watchdog took out of every pool, until `release`."""
    found = (prefs if prefs is not None else preferences()).get('quarantine')
    return found if isinstance(found, dict) else {}


def opted_in(prefs=None):
    """Risky settings you put in the pool with --risky."""
    found = (prefs if prefs is not None else preferences()).get('risky')
    return [name for name in found if name in RISKY] if isinstance(found, list) else []


def chosen_pool(specs, prefs=None):
    """The pool as chosen (saved, or by default every setting with power-saving/normal
    values but the RISKY ones), before quarantine."""
    prefs = prefs if prefs is not None else preferences()
    names = prefs.get('settings')
    if not isinstance(names, list):
        names = [name for name, spec in specs.items() if spec.levels and name not in RISKY]
    allowed = set(opted_in(prefs))
    return [name for name in names if name in specs and specs[name].levels and (name not in RISKY or name in allowed)]


def pool(specs=None):
    """Settings automatic experiments may vary: the chosen ones (RISKY ones only when
    opted in with --risky) but never quarantined ones."""
    specs = specs if specs is not None else catalog()
    prefs = preferences()
    blocked = quarantined(prefs)
    return [name for name in chosen_pool(specs, prefs) if name not in blocked]



def unrecommended(specs=None):
    """Settings recommendations leave as they are: every measured one outside
    pool() (risky ones not opted in with --risky, quarantined ones, and those
    taken out), so what the menu and the panel offer is what apply will set."""
    import report
    return tuple(sorted(set(report.SETTING) - set(pool(specs))))

def set_pool(names, risky=(), eligible=()):
    """Save `names` as the pool. Risky ones need to be in `risky` (asked for with
    --risky) or opted in before; quarantined ones need `release`. [(name, why)]
    for those left out."""
    prefs = preferences()
    already, blocked = set(opted_in(prefs)), quarantined(prefs)
    unknown = [name for name in names if name not in eligible]
    if unknown:
        raise SystemExit(f"not automatic settings: {', '.join(unknown)} (choose from {', '.join(eligible)})")
    kept, refused = [], []
    for name in dict.fromkeys(names):
        if name in RISKY and name not in risky and name not in already:
            refused.append((name, f'{name} is risky (PCIe/NVMe/fabric; the awake crashes): '
                                  f'`power-experiment pool add {name} --risky` to include it'))
            continue
        kept.append(name)  # A quarantined one stays chosen: `release` brings it back.
        if name in blocked:
            refused.append((name, f"{name} is quarantined ({blocked[name].get('reason', 'watchdog')}): "
                                  f'out until `power-experiment release {name}`'))
    risky_now = [name for name in kept if name in RISKY]
    prefer(settings=kept, risky=risky_now)
    return refused


def goal():
    """'battery' (the lowest draw) or 'comfort' (the most likely to feel especially good);
    both avoid combinations likely to earn a 👎."""
    chosen = preferences().get('goal')
    return chosen if chosen in GOALS else 'battery'


def next_block(names, present, run=None):
    """report.plan's next combination for a block of `run` (numpy: only the
    automatic runner needs it)."""
    import report
    now = time.time()
    minutes, events = report.load_range(now - 30 * 86400, now + 60)
    plan = report.plan(minutes, events, names, present=present, group=report.block_group(run, now) if run else None)
    return plan['choice'], {'accuracy': plan['accuracy'], 'cost': plan['cost'], 'allowed': plan['allowed'],
                            'combinations': plan['combinations'], 'value': plan.get('value')}


def recommended_values(specs, names, chosen=None, recommend=None):
    """The goal's best combination (recommend.json's) as the values of `names`
    it covers."""
    entry = ((recommend or recommendation)() or {}).get(chosen or goal()) or {}
    values = {}
    for assignment in entry.get('apply') or []:
        name, _, value = assignment.partition('=')
        if name in names and name in specs and specs[name].levels:
            levels = specs[name].levels()
            values[name] = levels[0] if value == 'saving' else levels[1] if value == 'normal' else value
    return values


def platform_version():
    """The kernel (major.minor) and the BIOS: what can change what settings save."""
    kernel = '.'.join(os.uname().release.split('.')[:2])
    return f"Linux {kernel}, BIOS {read(SYS / 'class/dmi/id/bios_version') or '?'}"


def retest_record(version=None):
    """{'version', 'tested'}: seconds of blocks since this kernel or BIOS first ran
    (at the first record, what was logged before counts as its: RETEST)."""
    version = version or platform_version()
    record = preferences().get('retest')
    if not isinstance(record, dict) or record.get('version') != version:
        record = {'version': version, 'tested': 0.0 if isinstance(record, dict) else float(RETEST)}
        prefer(retest=record)
    return record


def automatic(specs=None, clock=Clock, battery=on_battery, locked=screen_locked, resting=qol.resting,
              enabled=None, names=pool, choose=next_block, block=BLOCK, washout=WASHOUT, state_path=None,
              watchdog=None, ledger=None, best=None, version=platform_version):
    """Automatic experiments, for as long as they are enabled: each block, the
    combination of the pool's settings report.plan expects to sharpen the
    battery model most, among those unlikely to earn a 👎 while you are there.

    Pauses with the settings at stock on AC and for qol.COOLDOWN after a 👎
    (`qol resume` ends that early); a 👎 ends the block at once. Nothing
    changes for RESUME_SETTLE after a resume. A setting that can't be read
    (no Wi-Fi interface, power-profiles-daemon restarting) sits a block out; one
    that fails to set sits out BACKOFF, doubling, and after FAILURES_MAX failures
    the rest of the run. Risky settings are set last and hold (risky_allowed).
    The watchdog runs before the first block and every WATCH_EVERY seconds.

    While more testing would cost more battery than it saves (the plan's
    `value`, report.testing_value), it pauses with the goal's best combination
    (`best`: recommend.json's) set instead, and looks again every RECHECK or
    when the pool changes: the value of testing grows back as old blocks leave
    the models' window. After a kernel or BIOS change it tests RETEST first."""
    specs = specs if specs is not None else catalog()
    best = best or (lambda which: recommended_values(specs, which))
    enabled = enabled or (lambda: ENABLED.exists())
    ledger = ledger or Ledger(clock=clock.time)
    watchdog = watchdog or Watchdog(ledger=ledger)
    number, tested, started, owned, failing = 0, 0.0, clock.time(), set(), {}  # tested: s awake in blocks.
    run_id = f'auto-{int(started)}'
    resumed, tripped, pending, settled = None, None, {}, None
    retest = retest_record(version())
    base = {'run': run_id, 'mode': 'auto', 'label': 'Automatic experiments', 'started': started,
            'block_seconds': block, 'washout_seconds': washout}
    previous = load_state(state_path)  # Its value of testing, while fresh: the menu shows it in every state.
    if previous.get('worth') and started - previous.get('priced', -math.inf) < RECHECK:
        base.update(worth=previous['worth'], priced=previous['priced'])

    def stock_values(current):
        return {name: specs[name].stock_or_none(True) for name in current}

    def restore(which, patient=True):
        return restore_settings(specs, [name for name in which if name in specs], battery, ledger, by='auto',
                                patient=patient)

    def pause(reason, seconds, **fields):
        write_state(dict(base, status='paused', reason=reason, blocks=number, **fields), state_path)
        clock.sleep(seconds)

    # First, what a crashed run left goes back to stock: that needs no watchdog, and
    # a blind one waits below holding the settings lock (the reconciler can't help).
    clean = load_state(state_path).get('status') not in ('running', 'paused', 'restore-pending')
    if not clean:  # Its own catalog: an app it froze, say.
        command_restore(battery, ledger, state_path)
    pending = restore(set(leftovers(ledger, exclude=load_applied().get('values') or {})))
    # Then the kernel log since the last run (a blind watchdog waits).
    while enabled():
        found = watchdog.preflight(seen())
        if found is not None:
            break
        pause('the watchdog cannot read the kernel log', 60, **({'pending': sorted(pending)} if pending else {}))
    else:
        return
    if found:
        record = trip(found, specs, battery, ledger)
        write_state(finished(base, record['pending'], finished=clock.time(), blocks=0, watchdog=record), state_path)
        return
    ledger.start('auto')
    try:
        if clean:
            capture_profile(battery, now=clock.time())
        # The pool's settings and whatever an earlier run left start at stock.
        applied = load_applied().get('values') or {}
        current = names(specs)
        owned = set(current)
        pending = restore(set(leftovers(ledger, exclude=applied)) | owned)
        watched, paused_for = clock.time(), None
        _, mark = clock.suspended_since(None)
        while enabled():
            ledger.beat()
            suspended, mark = clock.suspended_since(mark)
            resumed = clock.time() if suspended else resumed
            if clock.time() - watched >= WATCH_EVERY:
                found, watched = watchdog.poll(watched - 10, seen()), clock.time()
                if found:
                    tripped = trip(found, specs, battery, ledger)
                    break
            current = names(specs)
            dropped = owned - set(current)
            if dropped:  # Dropped from the pool (or quarantined): back to stock.
                failed = restore(dropped)
                pending.update(failed)
                owned -= dropped - set(failed)
            owned |= set(current)
            reconcile_applied(specs, battery, ledger, exclude=set(current), now=clock.time(), resumed=resumed)
            if base.get('names') != current:
                snapshot = stock_values(current)
                base.update(name='+'.join(current), names=current, originals=snapshot, baseline=snapshot)
            if resumed is not None and clock.time() - resumed < RESUME_SETTLE:
                pause('settling after resume', 5)  # No restore: the post-resume hook just put things back.
                paused_for = None
                continue
            reason = ('no settings to test' if not current else 'on AC power' if not battery() else
                      'resting after your 👎' if resting() > 0 else None)
            if reason:
                if reason != paused_for or pending:  # Once on entering the pause, and until all is back.
                    pending = restore(owned | set(pending))
                paused_for = reason
                pause(reason, 15, **({'pending': sorted(pending)} if pending else {}))
                continue
            paused_for = None
            now, excluded = clock.time(), {}
            for name in current:
                failure = failing.get(name)
                if failure and now < failure['until']:
                    excluded[name] = f"failing: {failure['error']}"
                elif specs[name].read() is None:
                    excluded[name] = 'unavailable'
            usable = [name for name in current if name not in excluded]
            if not usable:
                # Wait, don't restore: a missing Wi-Fi interface after resume comes back by itself.
                pause(f'{specs[current[0]].label} unavailable' if len(current) == 1 else 'no setting available', 15,
                      excluded=excluded)
                continue
            if settled and (settled['names'] != current or now - settled['checked'] >= RECHECK):
                settled = None  # The pool changed, or time to look again.
            if settled is None:
                choice, plan = choose(usable, not locked(), run_id)
                worth = (plan or {}).pop('value', None) or {}
                if worth:
                    base.update(worth=worth, priced=now)
                if not worth.get('worth', True) and retest['tested'] >= RETEST:
                    settled = {'checked': now, 'names': current, 'at': -math.inf}
            if settled and (now - settled['at'] >= 300 or settled.get('goal') != goal()):
                try:  # The goal's combination: at once on a goal change, as recommend.json changes.
                    settled.update(target=best(usable), at=now, goal=goal())
                except Exception as error:  # noqa: BLE001 - stock meanwhile
                    print(f'power-experiment: no recommended combination: {error}', file=sys.stderr)
                    settled.update(target={}, at=now, goal=goal())
            values, held = {}, []
            for name in usable:
                value = (settled['target'].get(name, specs[name].stock_or_none(True)) if settled else
                         specs[name].levels()[0 if choice[name] else 1])
                if value is None:
                    continue
                if name in RISKY:
                    present = specs[name].read()
                    if not risky_allowed(name, present, value, ledger, now, specs[name].stock_or_none(True)):
                        value = present
                        held.append(name)
                values[name] = value
            busy = None
            for name in risky_last(list(values)):
                try:
                    if specs[name].read() != values[name]:
                        change(specs[name], values[name], ledger, 'auto')
                except Busy as error:
                    busy = error
                    break
                except Exception as error:  # noqa: BLE001 - this setting sits out; the others run
                    failure = failing.setdefault(name, {'count': 0})
                    failure['count'] += 1
                    failure['error'] = str(error)[:200]
                    failure['until'] = (math.inf if failure['count'] >= FAILURES_MAX else
                                        now + min(BACKOFF_MAX, BACKOFF * 2 ** (failure['count'] - 1)))
                    print(f'power-experiment: could not set {name} (failure {failure["count"]}): {error}',
                          file=sys.stderr)
                    pending.update(restore([name]))
                    excluded[name] = f'failing: {failure["error"]}'
            if busy:
                pause(f'power-lab busy ({busy})', 15)
                continue
            usable = [name for name in usable if name not in excluded]
            if not usable:
                pause(f'could not set {", ".join(specs[n].label for n in excluded)}', 15, excluded=excluded)
                continue
            if settled:
                pause(SETTLED, 60, settled=True, goal=settled['goal'], checked=settled['checked'],
                      value={n: values[n] for n in usable if n in values}, held=held, excluded=excluded)
                continue
            begin = clock.time()
            number += 1
            saving = {name: values[name] == specs[name].levels()[0] for name in usable}
            state = dict(base, status='running', block=number, blocks=number, tested=tested, plan=plan,
                         config={name: 'B' if saving[name] else 'A' for name in usable},
                         arm=''.join('B' if saving[name] else 'A' for name in usable),
                         value={name: values[name] for name in usable}, excluded=excluded, held=held,
                         block_start=begin, washout_until=begin + washout, block_end=begin + block)
            write_state(state, state_path)
            found, awake = None, begin
            while clock.time() < state['block_end']:
                clock.sleep(5)
                ledger.beat()
                suspended, mark = clock.suspended_since(mark)
                resumed = clock.time() if suspended else resumed
                if not suspended:  # A suspend's time isn't testing.
                    awake = min(clock.time(), state['block_end'])
                if clock.time() - watched >= WATCH_EVERY:
                    found, watched = watchdog.poll(watched - 10, seen()), clock.time()
                if (found or suspended or not battery() or not enabled() or names(specs) != current
                        or resting() > 0 or any(specs[name].read() != values[name] for name in usable)):
                    break
            tested += awake - begin
            if retest['tested'] < RETEST:  # Testing a new kernel or BIOS.
                retest['tested'] += awake - begin
                prefer(retest=retest)
            if clock.time() < state['block_end']:
                write_state(dict(state, block_end=clock.time()), state_path)  # Ended early.
            if found:
                tripped = trip(found, specs, battery, ledger)
                break
    finally:
        try:
            if tripped:
                pending = tripped['pending']
            else:
                pending = restore(owned | set(pending), patient=False)
        finally:
            ledger.end()
            write_state(finished({'run': run_id, 'mode': 'auto', 'label': 'Automatic experiments'}, pending,
                                 finished=clock.time(), blocks=number, tested=tested,
                                 originals=base.get('originals', {}),
                                 names=sorted(owned | set(pending)), watchdog=tripped), state_path)


def command_run():
    request = json.loads(REQUEST.read_text())
    specs = catalog(request.get('unit'), request.get('units'))
    names = request.get('names') or [request['name']]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Freezing every app is only acceptable while nobody is using them.
    only_locked = request.get('only_locked', False) or 'freeze-apps' in names
    # Risky settings: blocks of RISKY_HOLD, and experiment() enforces the hold and
    # the daily limit however often blocks are cut short (AC, suspend).
    block = max(BLOCK, RISKY_HOLD) if set(names) & set(RISKY) else BLOCK
    with ownership(wait=120) as mine:
        if not mine:
            sys.exit('power-experiment: another run owns the settings')
        experiment([specs[name] for name in names], request.get('minutes', 60), only_locked, block=block)


def load_state(path=None):
    return load_json(path or STATE)


def command_restore(battery=on_battery, ledger=None, state_path=None, stock=False, patient=False):
    """Put back what a run left: its settings (all of them with `stock`) to stock.
    A run that could not restore everything says 'restore-pending'; this retries it
    (ExecStopPost, the next run, the reconciler: `patient`). Pending settings, if any."""
    ledger = ledger or Ledger()
    state = load_state(state_path)
    status = state.get('status')
    pending = {}
    if status in ('running', 'paused', 'restore-pending'):
        specs = catalog(state.get('unit'), state.get('units'))
        names = set(state.get('pending') or []) | set(state.get('names') or []) | set(state.get('originals') or {})
        if not names and state.get('name'):
            names = {state['name']}
        pending = restore_settings(specs, names, battery, ledger, by='restore', captured=state.get('originals'),
                                   patient=patient)
        write_state(finished(state, pending, finished=state.get('finished') or time.time()), state_path)
    if stock:
        APPLIED.unlink(missing_ok=True)
        specs = catalog()
        more = restore_settings(specs, stock_names(specs, ledger), battery, ledger, by='stock')
        if more:
            write_state(finished(dict(state, names=sorted(more)), dict(pending, **more), finished=time.time()),
                        state_path)
        pending.update(more)
    return pending


def recommendation(now=None):
    """Each goal's best combination: battery-eta's recommend.json while under an
    hour old, else computed now (numpy; then marked 'computed')."""
    try:
        recommend = json.loads(RECOMMEND.read_text())
        if (now or time.time()) - recommend.get('updated', 0) <= 3600:
            return recommend
    except (OSError, ValueError):
        pass
    import eta
    import report
    now = time.time()
    return dict(eta.write_recommendations(*report.load_range(now - eta.WINDOW_DAYS * 86400, now + 60)), computed=True)


OWNED = 'experiments own the settings'


def not_applied(refused):
    """'power-experiment: not applied: name (why); name (why)' on stderr: the battery
    panel parses this line (panel_data.refused), so keep its form."""
    if refused:
        print('power-experiment: not applied: ' + '; '.join(refused), file=sys.stderr)


def running():
    """Whether a run (manual or automatic) owns the settings, by its state file."""
    return load_state().get('status') in ('running', 'paused')


def apply_goal(chosen, now=None, recommend=recommendation, risky=False):
    """Set `chosen`'s best measured combination — every setting in it, so it holds
    whatever the settings are now — unless automatic experiments own them. Risky
    settings stay at stock unless `risky`. The assignments set (None: a run owns them)."""
    if running():
        not_applied([OWNED])
        return None
    recommend = recommend(now)
    assignments = recommend.get(chosen, {}).get('apply')
    if not assignments:
        return []
    refused = {why.partition(' (')[0] for why in command_apply(assignments, risky=risky) or ()}
    applied = [a for a in assignments if a.partition('=')[0] not in refused]
    if recommend.get('computed'):  # Not what the menu or the panel showed: say what it was.
        print(f"power-experiment: recommend.json was stale, so the {chosen} combination was computed now; "
              f"applied: {', '.join(applied) or 'nothing'}", file=sys.stderr)
    return applied


def load_applied():
    """`apply`'s settings for this boot ({} after a reboot: they were runtime only)."""
    record = load_json(APPLIED)
    return record if record.get('boot') == boot_id() and isinstance(record.get('values'), dict) else {}


def reconcile_applied(specs, battery, ledger, exclude=(), now=None, explicit=False, resumed=None):
    """Applied settings follow the power source: on each change to battery they
    are set again, on each change to AC they go back to stock (udev's own for
    ABM and the profile). Between changes, on battery, risky ones that are not in
    place (the pre-sleep hook put them back to stock, and the post-resume one
    leaves them there) are set again: never within RESUME_SETTLE of `resumed`
    (the last resume) and only as risky_allowed allows, so a held one is tried
    again on every call (every minute). Quarantined settings are left out.
    {name: why} for those that failed (retried next time)."""
    record = load_applied()
    if not record:
        return {}
    on = battery()
    now = now or time.time()
    changed = explicit or record.get('on_battery') != on
    backoff = record.setdefault('failing', {})
    names = [name for name in record['values'] if name not in exclude
             and (explicit or now >= backoff.get(name, {}).get('until', 0))]
    if not changed:
        if not on:
            return {}
        names = [name for name in names if name in RISKY and name in specs
                 and specs[name].read() != record['values'][name]]
        if not names:
            return {}
    failures, skipped, later = {}, [], []
    if on:
        blocked, allowed = quarantined(), set(record.get('risky') or [])
        settling = resumed is not None and now - resumed < RESUME_SETTLE
        for name in risky_last(names):
            spec, value = specs.get(name), record['values'][name]
            if spec is None:
                failures[name] = 'unavailable'
                continue
            current = spec.read()
            if name in blocked or (name in RISKY and name not in allowed):
                skipped.append(name)
                continue
            if current != value and name in RISKY and (settling or not risky_allowed(
                    name, current, value, ledger, now, spec.stock_or_none(True), hold=not explicit)):
                later.append(name)  # At stock until allowed: tried again next time.
                continue
            try:
                if current is None:
                    raise RuntimeError('unavailable')
                if current != value:
                    change(spec, value, ledger, 'apply')
            except Exception as error:  # noqa: BLE001 - the rest first
                failures[name] = str(error)
    else:
        failures = restore_settings(specs, names, battery, ledger, by='apply')
    if changed and (skipped or later):
        print(f"power-experiment: left at stock for now: {', '.join(skipped + later)}", file=sys.stderr)
    for name in names:
        if name not in failures:
            backoff.pop(name, None)
            continue
        entry = backoff.setdefault(name, {'count': 0})
        entry['count'] += 1
        entry['until'] = now + min(BACKOFF_MAX, BACKOFF * 2 ** (entry['count'] - 1))
        if entry['count'] >= FAILURES_MAX and on:  # Gives up: back to stock and out of the record.
            print(f'power-experiment: giving up applying {name}: {failures[name]}', file=sys.stderr)
            record['values'].pop(name, None)
            backoff.pop(name, None)
            restore_settings(specs, [name], battery, ledger, by='apply')
    waiting = [name for name in record['values'] if name not in exclude and name not in names]  # Backing off.
    if not waiting and not [name for name in failures if name in record['values']]:
        record['on_battery'] = on
    write_state(record, APPLIED)
    return failures


def command_apply(assignments, risky=False, specs=None, battery=on_battery, ledger=None, resumed=None):
    """Set settings (`name=value`, value also saving|normal), e.g. the panel's best
    combination: while on battery, until reboot (reconcile puts them back to stock
    on AC and sets them again on battery). Risky settings move off stock only with
    `risky`, quarantined ones not at all, and not within RESUME_SETTLE of a resume
    (the reconciler sets them a minute later). The settings left at stock, with why."""
    if running():
        raise SystemExit('an experiment is running; stop it first')
    specs = specs if specs is not None else catalog()
    ledger = ledger or Ledger()
    blocked = quarantined()
    record = load_applied() or {'boot': boot_id(), 'values': {}, 'risky': []}
    values, allowed = dict(record['values']), set(record.get('risky') or [])
    to_stock, refused = [], []
    for assignment in assignments:
        name, _, value = assignment.partition('=')
        if name not in specs or not value:
            raise SystemExit(f'expected NAME=VALUE with NAME one of {", ".join(specs)}, got {assignment!r}')
        spec = specs[name]
        if value in ('saving', 'normal'):
            if spec.levels is None:
                raise SystemExit(f'{name} has no {value} value')
            value = spec.levels()[0 if value == 'saving' else 1]
        if value == spec.stock_or_none(True):
            values.pop(name, None)
            allowed.discard(name)
            to_stock.append(name)
        elif name in blocked:
            refused.append(f'{name} (quarantined: `power-experiment release {name}`)')
        elif name in RISKY and not risky:
            refused.append(f'{name} (risky: add --risky)')
        else:
            values[name] = value
            if name in RISKY:
                allowed.add(name)
    write_state(dict(record, values=values, risky=sorted(allowed), on_battery=None, updated=time.time()), APPLIED)
    if resumed is None and any(name in RISKY for name in values):
        resumed = Journal().last_resume()
    failures = restore_settings(specs, to_stock, battery, ledger, by='apply')
    failures.update(reconcile_applied(specs, battery, ledger, explicit=True, resumed=resumed))
    not_applied(refused)
    if not battery() and values:
        print('On AC: applied settings take effect on battery.')
    if failures:
        raise SystemExit('could not set ' + '; '.join(f'{name}: {why}' for name, why in failures.items()))
    return refused


def capture_profile(battery, now=None, settle=60):
    """Remember the profile as yours for this power source, unless power-experiment
    holds it (a run, an applied profile, a leftover) or the source changed under
    `settle` seconds ago (udev may still be switching)."""
    now = now or time.time()
    on = battery()
    source = load_json(SOURCE)
    if source.get('battery') != on:
        write_state({'battery': on, 'since': now}, SOURCE)
        return None
    if now - source.get('since', now) < settle or now - source.get('captured', 0) < 300:
        return None
    if 'profile' in (load_applied().get('values') or {}) or 'profile' in leftovers(Ledger()):
        return None
    try:
        value = profile_get()
    except Exception:  # noqa: BLE001 - power-profiles-daemon restarting
        return None
    write_state(dict(source, captured=now), SOURCE)
    if value in PROFILES and own_profile(on) != value:
        update_preferences(lambda prefs: prefs.update(
            own_profile=dict(prefs.get('own_profile') or {}, **{'battery' if on else 'ac': value})))
    return value


def command_reconcile(battery=on_battery, ledger=None):
    """power-experiment-reconcile.timer, while no run owns the settings: what a
    run left (restore-pending, a crash), applied settings after AC changes and
    after resume, and your own profile. While applied settings exist it is their
    watchdog: an incident since they were applied trips it (`trip`)."""
    ledger = ledger or Ledger()
    record, resumed = load_applied(), None
    if record.get('values'):
        watchdog = Watchdog(ledger=ledger)
        found = watchdog.poll(max(seen(), record.get('updated') or 0) - 10, seen())
        if found:
            return trip(found, catalog(), battery, ledger)['pending']
        resumed = watchdog.journal.last_resume()
    pending = command_restore(battery, ledger, patient=True)
    record = load_applied()
    specs = catalog()
    names = [name for name in leftovers(ledger, exclude=record.get('values') or {}) if name in specs]
    pending.update(restore_settings(specs, names, battery, ledger, by='reconcile', patient=True))
    pending.update(reconcile_applied(specs, battery, ledger, resumed=resumed))
    if not pending and load_state().get('status') not in ('running', 'paused', 'restore-pending'):
        capture_profile(battery)
    return pending


def release(names):
    """Let quarantined settings back into pools (`release all`: every one)."""
    released = []

    def update(prefs):
        quarantine = dict(prefs.get('quarantine') or {})
        for name in list(quarantine) if names == ['all'] else names:
            if quarantine.pop(name, None) is not None:
                released.append(name)
        prefs['quarantine'] = quarantine
    update_preferences(update)
    return released


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
    blocked = quarantined()
    return {'name': spec.name, 'label': spec.label, 'description': spec.description, 'root': spec.root,
            'visible': spec.visible, 'sleep_restored': spec.sleep_restored, 'ac_managed': spec.ac_managed,
            'current': current, 'alternative': alternative, 'saving': saving, 'normal': normal, 'error': error,
            'risky': spec.name in RISKY, 'quarantined': blocked.get(spec.name), 'stock': spec.stock_or_none(True)}


def command_enable():
    """Automatic experiments on, after the watchdog has looked at the kernel log: an
    unhandled incident trips it (and enable refuses once; enable again to override).
    A manual run is stopped (Conflicts=) and restores first."""
    ledger = Ledger()
    found = Watchdog(ledger=ledger).preflight(seen())
    if found is None:
        raise SystemExit('the kernel log cannot be read (journalctl -k), so the watchdog would be blind; not enabling')
    if found:
        with ownership(wait=30) as mine:
            record = trip(found, catalog(), on_battery, ledger, restore=mine)
        if record['disabled']:  # Not after a boot that died asleep or without a shutdown: noted, and on.
            raise SystemExit(f"watchdog: {record['reason']}; quarantined: "
                             f"{', '.join(record['quarantined']) or 'nothing'}. Automatic experiments stay off; "
                             '`power-experiment enable` again to start them anyway.')
    prefer(watchdog_seen=time.time())
    SAVED.mkdir(parents=True, exist_ok=True)
    ENABLED.touch()
    systemctl_wait('start', AUTO_UNIT, check=True)


def command_disable():
    """Automatic experiments off and every setting back to stock (applied ones too)."""
    ENABLED.unlink(missing_ok=True)
    # Applied settings end here, whatever happens next: what they changed is in the
    # ledger, so the reconciler puts it back even if the restore below can't run.
    APPLIED.unlink(missing_ok=True)
    systemctl_wait('stop', AUTO_UNIT)  # Its own stop restores what it changed (up to TimeoutStopSec).
    with ownership(wait=60) as mine:
        if not mine:
            raise SystemExit('a manual experiment owns the settings; `power-experiment stop` restores them')
        pending = command_restore(stock=True)
    if pending:
        raise SystemExit('could not restore ' + '; '.join(f'{name}: {why}' for name, why in pending.items())
                         + ' (the reconciler retries)')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='power-experiment', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('units')
    sub.add_parser('status')
    sub.add_parser('enable', help='start automatic experiments (they persist across logins)')
    sub.add_parser('disable', help='stop automatic experiments and put every setting back to stock')
    chosen = sub.add_parser('pool', help='settings automatic experiments may vary (pool add|remove NAME ...)')
    chosen.add_argument('names', nargs='*', metavar='name')
    chosen.add_argument('--risky', action='store_true', help=f"allow the risky settings ({', '.join(RISKY)})")
    sub.add_parser('quarantine', help='settings the watchdog took out, and why')
    freed = sub.add_parser('release', help='let quarantined settings back in (all: every one)')
    freed.add_argument('names', nargs='+', metavar='name')
    chosen_goal = sub.add_parser('goal', help='what the best combination optimizes: battery or comfort')
    chosen_goal.add_argument('goal', nargs='?', choices=GOALS)
    chosen_goal.add_argument('--apply', action='store_true',
                             help="also set that goal's best measured combination now (not during experiments)")
    chosen_goal.add_argument('--risky', action='store_true', help='with --apply: risky settings too')
    sub.add_parser('auto', help='the power-experiments.service body')
    sub.add_parser('resume', help='restart automatic experiments if enabled (after a manual run)')
    start = sub.add_parser('start', help='randomize one setting, or several together')
    start.add_argument('names', nargs='+', metavar='name')
    start.add_argument('--minutes', type=int, default=60)
    start.add_argument('--unit')
    start.add_argument('--only-locked', action='store_true')
    start.add_argument('--risky', action='store_true', help=f'needed for {", ".join(RISKY)} (blocks of '
                                                            f'{RISKY_HOLD // 60} min)')
    start.add_argument('--keep', action='append',
                       help='freeze-apps: leave scopes matching this (default: t3code, so agents keep running)')
    sub.add_parser('stop')
    apply = sub.add_parser('apply', help='set settings while on battery, e.g. cpu-cap=saving (until reboot)')
    apply.add_argument('assignments', nargs='+', metavar='name=value')
    apply.add_argument('--risky', action='store_true', help=f'also move {", ".join(RISKY)} off stock')
    boot = sub.add_parser('next-boot')
    boot.add_argument('arm', choices=['default', 'psr'])
    sub.add_parser('boot-status')
    test = sub.add_parser('test-freeze', help='freeze the freeze-apps targets now for a few seconds, then thaw')
    test.add_argument('--seconds', type=float, default=5)
    test.add_argument('--keep', action='append')
    restore = sub.add_parser('restore', help="put a run's leftovers back to stock (--stock: every setting)")
    restore.add_argument('--stock', action='store_true')
    sub.add_parser('reconcile', help='the power-experiment-reconcile.timer body')
    sub.add_parser('run')
    args = parser.parse_args(argv)
    if args.command == 'list':
        print(json.dumps([describe(spec) for spec in catalog().values()]))
    elif args.command == 'units':
        print(json.dumps(freezable_units()))
    elif args.command == 'status':
        state = load_state()
        prefs = preferences()
        state.update(locked=screen_locked(), on_battery=on_battery(), freezer=freezer_report(state.get('units')),
                     uncovered=uncovered(), automatic={'enabled': ENABLED.exists(), 'settings': pool(), 'goal': goal(),
                                                       'risky': opted_in(prefs), 'quarantine': quarantined(prefs),
                                                       'last_trip': prefs.get('last_trip')},
                     applied=load_applied().get('values') or {}, resting=qol.resting())
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
        blocked = quarantined()
        for name in names:
            if name not in known:
                parser.error(f'unknown experiment {name!r}' + (' (needs --unit)' if name == 'freeze' else ''))
            if name in blocked:
                parser.error(f'{name} is quarantined by the watchdog: `power-experiment release {name}` first')
            if name in RISKY and not args.risky:
                parser.error(f'{name} is risky (PCIe/NVMe/fabric, linked to awake crashes): add --risky')
        write_state({'names': names, 'minutes': max(8, min(args.minutes, 24 * 60)), 'unit': args.unit,
                     'units': units, 'only_locked': args.only_locked, 'risky': args.risky}, REQUEST)
        systemctl_wait('restart', 'power-experiment.service', check=True)
    elif args.command == 'apply':
        with ownership(wait=30) as mine:
            if not mine:
                raise SystemExit('an experiment is running; stop it first')
            command_apply(args.assignments, risky=args.risky)
    elif args.command == 'enable':
        command_enable()
    elif args.command == 'disable':
        command_disable()
    elif args.command == 'pool':
        if args.names:
            eligible = [name for name, spec in catalog().items() if spec.levels]
            names, current = args.names, chosen_pool(catalog())
            if names[0] == 'add':
                names = list(current) + names[1:]
            elif names[0] == 'remove':
                names = [name for name in current if name not in names[1:] and name in eligible]
            asked = set(args.names[1:] if args.names[0] in ('add', 'remove') else args.names)
            refused = set_pool(names, risky=asked if args.risky else (), eligible=eligible)
            refused = [why for name, why in refused if name in asked]  # Not old risky names, silently dropped.
            if refused:
                print(' '.join(pool()))
                raise SystemExit('power-experiment: not in the pool: ' + '; '.join(refused))
        print(' '.join(pool()))
    elif args.command == 'quarantine':
        print(json.dumps(quarantined(), indent=1))
    elif args.command == 'release':
        print(' '.join(release(args.names)) or 'nothing quarantined by that name')
    elif args.command == 'goal':
        if args.goal:
            prefer(goal=args.goal)
            if args.apply:
                if running() and load_state().get('settled'):  # The paused runner sets the goal's combination.
                    print('power-experiment: paused experiments switch to its combination within a minute',
                          file=sys.stderr)
                elif running():  # At once, not after waiting for the lock a run holds for as long as it runs.
                    not_applied([OWNED])
                else:
                    with ownership(wait=30) as mine:
                        if mine:
                            apply_goal(args.goal, risky=args.risky)
                        else:
                            not_applied([OWNED])
        print(goal())
    elif args.command == 'auto':
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        with ownership(wait=120) as mine:
            if not mine:
                sys.exit('power-experiment: another run owns the settings')
            automatic()
    elif args.command == 'resume':
        if ENABLED.exists():
            run(['systemctl', '--user', '--no-block', 'start', AUTO_UNIT], check=False)
    elif args.command == 'stop':
        systemctl_wait('stop', 'power-experiment.service')
    elif args.command == 'restore':
        with ownership(wait=30) as mine:
            if not mine:
                raise SystemExit('a run owns the settings (it restores them when it stops)')
            pending = command_restore(stock=args.stock)
        if pending:  # Not an error for ExecStopPost: the state says restore-pending and the reconciler retries.
            print('power-experiment: restore pending: ' + '; '.join(f'{n}: {w}' for n, w in pending.items()),
                  file=sys.stderr)
    elif args.command == 'reconcile':
        with ownership() as mine:
            if mine:
                command_reconcile()
    elif args.command == 'next-boot':
        print(lab('next-boot', args.arm))
    elif args.command == 'boot-status':
        print(json.dumps({'psr': psr_arm()}))
    elif args.command == 'run':
        command_run()


if __name__ == '__main__':
    if shutil.which('systemctl') is None:
        sys.exit('power-experiment needs systemd')
    main()
