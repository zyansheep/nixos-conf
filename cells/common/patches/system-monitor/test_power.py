import contextlib
import io
import json
import os
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import power
import store
from power import (PRIOR, Deltas, FastSampler, GpuClients, IdleStates, Monitor, PowerLog, attribute, battery_watts,
                   clock_means, clock_split, clocks, cpu_frequency, device_deltas, experiment_assignment, idle_deltas,
                   interrupts, load_attribution, log_footer, network, own_cpu, parse_fdinfo, settings, subtract,
                   summarize, TICKS)


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class PowerTests(unittest.TestCase):
    def test_battery_sign_and_current_voltage_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery = Path(tmp)
            put(battery / 'status', 'Discharging')
            put(battery / 'current_now', '500000')     # 0.5 A
            put(battery / 'voltage_now', '16000000')   # 16 V
            self.assertEqual(battery_watts(battery), ('Discharging', 8.0))
            put(battery / 'status', 'Charging')
            self.assertEqual(battery_watts(battery), ('Charging', -8.0))
            put(battery / 'power_now', '3000000')
            put(battery / 'status', 'Not charging')
            self.assertEqual(battery_watts(battery), ('Not charging', 0.0))

    def test_apst_is_the_drives_runtime_limit_not_the_boot_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys = Path(tmp)
            put(sys / 'module/nvme_core/parameters/default_ps_max_latency_us', '0\n')
            self.assertEqual(settings(sys)['nvme_apst_us'], '0')
            put(sys / 'class/nvme/nvme0/power/pm_qos_latency_tolerance_us', '100000\n')
            self.assertEqual(settings(sys)['nvme_apst_us'], '100000')

    def test_the_cpu_cap_its_maximum_and_the_gpu_dpm_level_are_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys = Path(tmp)
            put(sys / 'devices/system/cpu/cpu0/cpufreq/scaling_max_freq', '2000000\n')
            put(sys / 'devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq', '4972435\n')
            put(sys / 'class/drm/card1/device/power_dpm_force_performance_level', 'low\n')
            values = settings(sys)
            self.assertEqual((values['max_mhz'], values['cpu_max_mhz'], values['gpu_dpm']), (2000, 4972, 'low'))

    def test_throttle_is_on_only_while_its_process_lives(self):
        import json, os
        from power import throttle_state
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'throttle.json'
            self.assertEqual(throttle_state(path), 'off')
            path.write_text(json.dumps({'pid': os.getpid()}))
            self.assertEqual(throttle_state(path), 'on')
            path.write_text(json.dumps({'pid': 2 ** 22 + 12345}))  # Above pid_max: long gone.
            self.assertEqual(throttle_state(path), 'off')

    def test_interrupt_names_skip_chip_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            put(Path(tmp) / 'interrupts', '           CPU0       CPU1\n'
                '  88:          3          4  IR-PCI-MSIX-0000:02:00.0    1-edge      nvme0q1\n'
                ' LOC:         10         20   Local timer interrupts\n')
            self.assertEqual(interrupts(Path(tmp)), {'88 nvme0q1': 7, 'LOC': 30})

    def test_gpu_clients_dedupe_shared_fds_and_skip_new_clients(self):
        self.assertEqual(parse_fdinfo('drm-client-id:\t7\ndrm-engine-gfx:\t5 ns\ndrm-engine-capacity-gfx:\t1\n'),
                         ('7', {'gfx': 5}))
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid in (10, 11):
                (proc / str(pid) / 'fd').mkdir(parents=True)
                (proc / str(pid) / 'fd' / '3').symlink_to('/dev/dri/renderD128')
            def info(gfx, dec):
                text = f'drm-client-id:\t7\ndrm-engine-gfx:\t{gfx} ns\ndrm-engine-dec:\t{dec} ns\n'
                for pid in (10, 11):  # A forked child shares the parent's DRM client.
                    put(proc / str(pid) / 'fdinfo' / '3', text)
            processes = {(10, 0): ('a', 0, 0, 'Game'), (11, 0): ('b', 0, 0, 'Game')}
            gpu = GpuClients(proc)
            info(0, 0)
            self.assertEqual(gpu.sample(0, processes), {})
            info(2 * 10**9, 10**9)
            self.assertEqual(gpu.sample(10, processes), {'Game': {'gpu': 2.0, 'video': 1.0}})
            put(proc / '10' / 'fdinfo' / '3', 'drm-client-id:\t8\ndrm-engine-gfx:\t99000000000 ns\n')
            gpu.rescan = 0
            usage = gpu.sample(20, {(10, 0): processes[(10, 0)]})
            self.assertNotIn('Game', usage)  # Client 8's lifetime total is not this interval's.

    def test_deltas_ignore_pid_reuse_and_count_new_processes(self):
        deltas = Deltas()
        apps = {(1, 0): 'old', (1, 500 * TICKS): 'new', (2, 0): 'early'}
        self.assertEqual(deltas.sample(100, {(1, 0): {'io_w': 10}, (2, 0): {'io_w': 5}}, apps), {})
        usage = deltas.sample(600, {(1, 500 * TICKS): {'io_w': 7}, (2, 0): {'io_w': 9}}, apps)
        self.assertEqual(dict(usage['new']), {'io_w': 7})
        self.assertEqual(dict(usage['early']), {'io_w': 4})
        self.assertNotIn('old', usage)
        # Re-priming after a suspend (previous state present) or a racing scan
        # that lacks a process's app must not crash.
        usage = deltas.sample(700, {(2, 0): {'io_w': 20}, (3, 0): {'io_w': 1}}, {(3, 0): 'x'})
        self.assertNotIn('early', usage)

    def test_attribution_parameters_come_from_battery_etas_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.json'
            self.assertIsNone(load_attribution(path))
            path.write_text(json.dumps({'version': 2, 'attribution': {'chip': 1.4, 'floor': 5.0, 'junk': 1}}))
            params = load_attribution(path)
            self.assertEqual((params['chip'], params['floor'], params['bl']), (1.4, 5.0, PRIOR['bl']))
            self.assertNotIn('junk', params)
            idle = {'idle_ghz:balanced': 1.5, 'idle_ghz:balanced:ac': 1.9, 'idle_ghz_active': 1.8, 'own:aspm': 0.0}
            path.write_text(json.dumps({'version': 3, 'attribution': {'chip': 1.4, **idle}}))
            params = load_attribution(path)
            self.assertEqual({k: params.get(k) for k in idle}, dict(idle, **{'own:aspm': None}))  # The clocks only

    def test_the_clock_is_measured_from_the_profiles_idle_clock(self):
        params = dict(PRIOR, clock=3.0, idle_ghz=1.0, **{'idle_ghz:balanced': 1.5, 'idle_ghz:balanced:ac': 1.9})
        split = lambda *args, **kwargs: tuple(round(w, 9) for w in clock_split(*args, **kwargs))
        self.assertEqual(split(params, 2.0), (3.0, 3.0))                   # Pooled: 1 GHz idle
        self.assertEqual(split(params, 2.0, 'balanced'), (3.0, 1.5))       # Balanced idles at 1.5 GHz
        self.assertEqual(split(params, 2.0, 'balanced', ac=True), (3.0, 0.3))
        self.assertEqual(split(params, 1.2, 'balanced'), (2.1, 0.0))      # Below its idle: less baseline
        self.assertEqual(split(params, 2.0, 'performance'), (3.0, 3.0))    # Unknown profile: pooled
        self.assertEqual(split(params, 2.0, 'balanced', active=3.0), (3.0, 1.5))  # No active model yet
        active = dict(params, idle_ghz_active=2.0, clock_active=0.5)
        self.assertEqual(split(active, 2.0, 'balanced', active=3.0), (3.0, 0.5))
        self.assertEqual(split(active, 2.0, 'balanced', active=1.0), (2.5, 0.0))
        self.assertEqual(split(active, 2.0, 'balanced'), (3.0, 1.5))       # A record without it

    def test_attribution_uses_the_records_profile_and_active_clock(self):
        params = dict(PRIOR, **{'idle_ghz:balanced': 1.5, 'idle_ghz_active': 2.0})
        record = {'dt': 10, 'status': 'Discharging', 'bat': {'w': 20.0}, 'soc': {'w': 10.0},
                  'cpu': {'busy_s': 10.0, 'mhz': 1200}, 'apps': {'Browser': {'cpu': 10.0}},
                  'settings': {'platform_profile': 'balanced'}}
        # 1.2 GHz is below balanced's idle clock: 0.3 GHz less baseline, 1.3 × (3 W + 3 W/GHz × 0.7 GHz) × 10 s.
        self.assertAlmostEqual(attribute(record, params)['Processor baseline'], 66.3)
        record['settings']['platform_profile'] = None  # Unknown: the pooled idle clock, 1 GHz
        self.assertAlmostEqual(attribute(record, params)['Processor baseline'], 78.0)
        record['settings']['platform_profile'] = 'balanced'
        record['cpu']['mhz_active'] = 2500  # The active clock instead: above its idle, so the baseline is whole
        rows = attribute(record, params)
        self.assertAlmostEqual(rows['Processor baseline'], 78.0)
        self.assertAlmostEqual(sum(rows.values()), 200.0)
        record['bat']['w'] = 9.0  # Less than the chip at its cost per watt: the parts still add up to it.
        self.assertAlmostEqual(sum(attribute(record, params).values()), 90.0)

    def test_attribution_splits_chip_by_activity_and_platform_by_model(self):
        params = dict(PRIOR)   # floor 3 W + 3 W/GHz (to the 1 GHz idle clock), 0.12 W/(core·GHz²), ×1.3 per chip watt
        record = {'dt': 10, 'status': 'Discharging', 'bat': {'w': 20.0}, 'soc': {'w': 10.0},
                  'cpu': {'busy_s': 10.0, 'mhz': 2000},
                  'apps': {'Browser': {'cpu': 6.0}, 'Game': {'cpu': 2.0, 'gpu': 2.0}},
                  'display': {'bl': .5}, 'net': {'wlan0': {'rx': 0, 'tx': 0}}}
        rows = attribute(record, params)
        self.assertAlmostEqual(rows['Processor baseline'], 78.0)        # 1.3 × (3 W + 3 W/GHz × 1 GHz) × 10 s
        self.assertAlmostEqual(rows['Kernel'] / rows['Browser'], 1 / 3)  # Unowned busy CPU; the clock above idle by CPU time
        self.assertGreater(rows['Game'], rows['Kernel'])                # Same CPU time, plus GPU time
        self.assertAlmostEqual(rows['Display'], 15.0)                    # 3 W × 0.5 brightness × 10 s
        self.assertAlmostEqual(rows['Rest of system'], 55.0)
        self.assertAlmostEqual(sum(rows.values()), 200.0)                # Equals battery energy
        record['bat']['w'] = 13.5  # Less platform power than modelled: shrink, no negative rest.
        rows = attribute(record, params)
        self.assertAlmostEqual(rows['Display'], 5.0)
        self.assertAlmostEqual(rows['Rest of system'], 0.0)
        record['status'] = 'Charging'
        rows = attribute(record, params)
        self.assertNotIn('Display', rows)
        self.assertAlmostEqual(rows['Processor baseline'], 60.0)        # On AC: chip watts only
        record['cpu']['mhz'] = 800  # Below the idle clock: the whole clock term is baseline.
        self.assertAlmostEqual(attribute(record, params)['Processor baseline'], 54.0)  # (3 W + 3 W/GHz × 0.8 GHz) × 10 s

    def test_experiment_assignment_marks_washout_and_ignores_paused_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'experiment.json'
            self.assertIsNone(experiment_assignment(path, 0, 10))
            state = {'status': 'running', 'run': 'r1', 'name': 'aspm', 'arm': 'B', 'value': 'powersupersave',
                     'block': 3, 'block_start': 100, 'washout_until': 160, 'block_end': 340}
            path.write_text(json.dumps(state))
            self.assertTrue(experiment_assignment(path, 150, 160)['washout'])
            self.assertFalse(experiment_assignment(path, 200, 210)['washout'])
            self.assertTrue(experiment_assignment(path, 90, 100)['washout'])   # Straddles the switch
            path.write_text(json.dumps(dict(state, status='paused')))
            self.assertIsNone(experiment_assignment(path, 200, 210))

    def test_summary_prefers_battery_time_and_reports_log(self):
        window = [(1, 10, False, {'Browser': 100.0}), (2, 10, True, {'Browser': 50.0, 'Display': 30.0})]
        menu = summarize(window, 3, (3, 3600 * 5, None))
        self.assertTrue(menu['title'].startswith('Battery use · last 1 min on battery · 8.0 W'))
        self.assertEqual(menu['rows'][0], {'name': 'Browser', 'value': ' 5.0 W  62%'})
        self.assertIn('3 samples · 5.0 h', menu['footer'])
        self.assertIn('chip power by app', summarize(window[:1], 3, (0, 0, None))['title'])
        self.assertEqual(summarize([], 3, (0, 0, None))['rows'], [])
        self.assertEqual(log_footer(0, 0, None), 'Power log: no samples yet')

    def test_log_rotates_compresses_and_recovers_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            old = '2000-01-01'
            put(directory / f'{old}.jsonl', json.dumps({'v': 1, 't0': 1, 't1': 11, 'dt': 10}) + '\n{"torn')
            log = PowerLog(directory)
            compressed = [p for p in directory.iterdir() if p.name.startswith(old)]
            self.assertEqual(len(compressed), 1)
            self.assertIn(compressed[0].suffix, ('.zst', '.gz'))
            now = time.time()
            log.write({'v': 1, 'event': 'start', 't1': now})
            for i in range(3):
                log.write({'v': 1, 't0': now + i * 10, 't1': now + (i + 1) * 10, 'dt': 10})
            log.close()
            self.assertEqual(log.totals(), (4, 40, 1))
            self.assertEqual(len(PowerLog(directory).recent(now + 15)), 2)
            reopened = PowerLog(directory)
            self.assertEqual(reopened.totals(), (4, 40, 1))  # Today's file rescanned, old day indexed.


    def test_clocks_are_read_together_even_across_a_suspend(self):
        boots = iter([100.0, 3700.0, 3700.0, 3700.1])  # A suspend between the first uptime and the wall-clock read.
        walls = iter([1000.0, 4600.0])
        self.assertEqual(clocks(lambda: next(boots), lambda: next(walls)), (3700.1, 4600.0))

    def test_the_active_clock_leaves_out_idle_cpus_and_the_collectors_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys, proc = Path(tmp) / 'sys', Path(tmp) / 'proc'
            for cpu, current in enumerate([4000000, 406000, 2000000, 406000, 3000000]):
                put(sys / f'devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq', f'{current}\n')
                put(sys / f'devices/system/cpu/cpu{cpu}/cpufreq/scaling_min_freq', '406000\n')
            put(proc / 'thread-self/stat', '42 (waybar (mon) x) R ' + ' '.join(str(i) for i in range(4, 39)) + ' 0 0 0\n')
            self.assertEqual(own_cpu(proc), 0)  # Field 39, after a comm with spaces and parentheses.
            mean, peak, active, n = cpu_frequency(sys, own_cpu(proc))
            self.assertAlmostEqual(mean, (4000 + 406 + 2000 + 406 + 3000) / 5)  # As ever: every CPU.
            self.assertEqual((peak, active, n), (4000, 2500, 2))  # cpu0 is ours; cpu1 and cpu3 idle.
            self.assertEqual(cpu_frequency(sys, own=2)[2:], (3500, 2))
            self.assertIsNone(own_cpu(Path(tmp) / 'nowhere'))
            put(sys / 'devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', '406000\n')
            put(sys / 'devices/system/cpu/cpu2/cpufreq/scaling_cur_freq', '406000\n')
            put(sys / 'devices/system/cpu/cpu4/cpufreq/scaling_cur_freq', '406000\n')
            self.assertEqual(cpu_frequency(sys, 0)[2:], (None, 0))
            self.assertEqual(cpu_frequency(Path(tmp) / 'nowhere'), (None, None, None, 0))  # No cpufreq.
            put(sys / 'devices/system/cpu/cpu0/cpufreq/scaling_max_freq', '5000000\n')
            put(sys / 'devices/system/cpu/cpu0/cpufreq/scaling_min_freq', '1116000\n')
            self.assertEqual(settings(sys)['min_mhz'], 1116)  # The floor follows the profile.

    def test_record_clock_is_time_weighted_over_samples_with_an_active_cpu(self):
        samples = [(2.0, 1000, 3000, 3000, 2), (6.0, 800, 1200, 1500, 1), (2.0, 600, 600, None, 0)]
        self.assertEqual(clock_means(samples), {'mhz': 800, 'mhz_max': 3000, 'mhz_active': 1875, 'active_n': 1.0})
        self.assertEqual(clock_means([(0.0, 600, 600, None, 0)]), {'mhz': 600, 'mhz_max': 600, 'active_n': 0.0})
        self.assertEqual(clock_means([(0.0, 900, 900, 2000, 1)])['mhz_active'], 2000)  # The only sample: weight 0.
        self.assertEqual(clock_means([]), {})

    def test_counter_deltas_are_never_negative_and_recreated_devices_restart(self):
        self.assertEqual(subtract({'iowait': 5.0, 'user': 9.0, 'ctxt': 10}, {'iowait': 5.5, 'user': 4.0, 'ctxt': 4}),
                         {'iowait': 0, 'user': 5.0, 'ctxt': 6})  # /proc/stat iowait can step back.
        self.assertEqual(subtract({'ctxt': 30}, {'ctxt': 10}, 1.5), {'ctxt': 30})
        before = {'wlan0': {'rx': 1000, 'tx': 500, 'state': 'up'}, 'tun0': {'rx': 9000, 'tx': 9000, 'state': 'up'}}
        after = {'wlan0': {'rx': 1600, 'tx': 700, 'state': 'up'}, 'tun0': {'rx': 300, 'tx': 9100, 'state': 'up'},
                 'usb0': {'rx': 5, 'tx': 5, 'state': 'up'}}
        self.assertEqual(device_deltas(after, before), {'wlan0': {'rx': 600, 'tx': 200}, 'tun0': {}, 'usb0': {}})
        later = {'tun0': {'rx': 400, 'tx': 9150, 'state': 'up'}}
        self.assertEqual(device_deltas(later, after), {'tun0': {'rx': 100, 'tx': 50}})  # From the new baseline.
        with tempfile.TemporaryDirectory() as tmp:
            sys = Path(tmp)
            for name, value in (('rx_bytes', '10'), ('tx_bytes', '20'), ('rx_packets', '1'), ('tx_packets', '2')):
                put(sys / 'class/net/wlan0/statistics' / name, value)
            put(sys / 'class/net/wlan0/operstate', 'up')
            put(sys / 'class/net/eth0/statistics/rx_bytes', '10')  # Half gone: no reading, not zeros.
            self.assertEqual(network(sys), {'wlan0': {'rx': 10, 'tx': 20, 'rxp': 1, 'txp': 2, 'state': 'up'}})

    def test_idle_residency_survives_a_cpu_going_offline_and_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys = Path(tmp)
            for cpu in (0, 1):
                put(sys / f'devices/system/cpu/cpu{cpu}/cpuidle/state0/name', 'C1')
                put(sys / f'devices/system/cpu/cpu{cpu}/cpuidle/state0/time', '5000000')
            states = IdleStates(sys)
            first = states.read()
            put(sys / 'devices/system/cpu/cpu0/cpuidle/state0/time', '7000000')
            (sys / 'devices/system/cpu/cpu1/cpuidle/state0/time').unlink()  # cpu1 offline
            second = states.read()
            self.assertEqual(idle_deltas(second, first), {'C1': 2.0})
            put(sys / 'devices/system/cpu/cpu1/cpuidle/state0/time', '9000000')  # Back, with all of its history.
            self.assertEqual(idle_deltas(states.read(), second), {'C1': 0.0})

    def test_device_paths_are_found_again_after_failed_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys = Path(tmp)
            put(sys / 'class/power_supply/BAT0/type', 'Battery')
            put(sys / 'class/power_supply/BAT0/status', 'Discharging')
            put(sys / 'class/power_supply/BAT0/power_now', '8000000')
            put(sys / 'class/hwmon/hwmon2/name', 'amdgpu')
            put(sys / 'class/hwmon/hwmon2/power1_input', '5000000')
            fast = FastSampler(sys)
            fast.sample()
            self.assertEqual(fast.take()[0]['bat']['w'], 8.0)
            # The battery and the hwmon directory come back under new names.
            (sys / 'class/power_supply/BAT0').rename(sys / 'class/power_supply/BAT1')
            (sys / 'class/hwmon/hwmon2').rename(sys / 'class/hwmon/hwmon5')
            for _ in range(power.MISSES + 1):
                fast.sample()
            values, status = fast.take()
            self.assertEqual((values['bat']['w'], values['soc']['w'], status), (8.0, 5.0, 'Discharging'))
            self.assertEqual(fast.battery.name, 'BAT1')

    def test_gpu_clients_count_new_clients_from_zero_and_cap_at_the_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            counts = {}

            class Clients(GpuClients):
                def fd_count(self, pid):
                    return counts.get(pid, 1)

            def client(pid, fd, cid, gfx, compute=0):
                (proc / str(pid) / 'fd').mkdir(parents=True, exist_ok=True)
                link = proc / str(pid) / 'fd' / str(fd)
                if not link.is_symlink():
                    link.symlink_to('/dev/dri/renderD128')
                put(proc / str(pid) / 'fdinfo' / str(fd),
                    f'drm-client-id:\t{cid}\ndrm-engine-gfx:\t{gfx} ns\ndrm-engine-compute:\t{compute} ns\n')

            client(10, 3, 1, 0)
            gpu = Clients(proc)
            app = {(10, 0): ('a', 0, 0, 'Game')}
            self.assertEqual(gpu.sample(100, app), {})
            # Game opens a second client: its fd count changes, so it is found at once and counts from zero.
            client(10, 4, 2, 3 * 10**9)
            counts[10] = 2
            self.assertEqual(gpu.sample(110, app), {'Game': {'gpu': 3.0}})
            # A process started since the last sample: its client counts from zero too.
            client(11, 3, 3, 10**9)
            both = {**app, (11, int(115 * TICKS)): ('b', 0, 0, 'Viewer')}
            self.assertEqual(gpu.sample(120, both), {'Viewer': {'gpu': 1.0}})
            # gfx and compute rings run concurrently; jobs overlap: capped at the interval.
            client(10, 4, 2, 13 * 10**9, 9 * 10**9)
            self.assertEqual(gpu.sample(130, both)['Game'], {'gpu': 10})
            # An older process whose client only the periodic rescan found: unknown history, skipped.
            client(12, 3, 4, 50 * 10**9)
            gpu.rescan = 0
            usage = gpu.sample(140, {**both, (12, 0): ('c', 0, 0, 'Old')})
            self.assertNotIn('Old', usage)

    def test_a_corrupt_archive_day_is_reported_not_fatal(self):
        if power.zstd is None:
            self.skipTest('no zstd')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            put(directory / '2000-01-01.jsonl.zst', 'not zstd at all')
            with mock.patch('power.stderr', new_callable=io.StringIO) as err:
                log = PowerLog(directory)
            self.assertIn('ZstdError', err.getvalue())
            self.assertIn('2000-01-01.jsonl.zst', err.getvalue())
            self.assertEqual(log.index['days']['2000-01-01']['records'], 0)


def fake_machine(root, cpus=4):
    """A /sys and /proc with what the collector reads: a battery, amdgpu power,
    cpufreq, /proc/stat."""
    sys, proc = root / 'sys', root / 'proc'
    battery = sys / 'class/power_supply/BAT1'
    for name, value in (('type', 'Battery'), ('status', 'Discharging'), ('power_now', '10000000'),
                        ('capacity', '50'), ('charge_now', '2000000'), ('voltage_now', '15500000')):
        put(battery / name, value)
    put(sys / 'class/power_supply/ACAD/type', 'Mains')
    put(sys / 'class/power_supply/ACAD/online', '0')
    put(sys / 'class/hwmon/hwmon3/name', 'amdgpu')
    put(sys / 'class/hwmon/hwmon3/power1_input', '8000000')
    for cpu in range(cpus):
        put(sys / f'devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq', '406000' if cpu % 2 else '3000000')
        put(sys / f'devices/system/cpu/cpu{cpu}/cpufreq/scaling_min_freq', '406000')
    put(proc / 'thread-self/stat', '7 (collector) R ' + ' '.join('0' for _ in range(4, 39)) + ' 0 0\n')
    stat(proc, 0)
    return sys, proc


def stat(proc, busy_ticks, idle_ticks=0):
    put(proc / 'stat', f'cpu  {busy_ticks} 0 0 {idle_ticks} 0 0 0 0 0 0\nctxt 100\nintr 50\n')


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sys, self.proc = fake_machine(root)
        self.log, self.db = root / 'state/power', root / 'state/power.sqlite3'
        self.uptime, self.asleep = [1000.0], [0.0]  # Boot clock; the part of it spent suspended.
        self.patches = [mock.patch('power.qol.votes', return_value=[]),
                        mock.patch('power.Commands.read', return_value={}),
                        mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'xdg')})]
        for patch in self.patches:
            patch.start()
        self.wall = time.time()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def monitor(self):
        return Monitor(self.log, sys=self.sys, proc=self.proc, db=self.db, clock=lambda: self.uptime[0],
                       asleep=lambda: self.asleep[0])

    def tick(self, monitor, seconds=2, busy=None):
        self.uptime[0] += seconds
        if busy is not None:
            stat(self.proc, busy)
        return monitor.update(self.uptime[0], self.wall + self.uptime[0] - 1000, {})

    def rows(self, sql):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql).fetchall()

    def test_records_carry_the_active_clock_and_its_floor(self):
        monitor = self.monitor()
        for i in range(6):
            self.tick(monitor, busy=i * 2 * TICKS)  # One core busy.
        monitor.close()
        (record,) = monitor.log.recent(0)
        self.assertEqual(record['cpu']['mhz'], round((3000 + 406 + 3000 + 406) / 4))  # Unchanged meaning.
        self.assertEqual((record['cpu']['mhz_active'], record['cpu']['active_n']), (3000, 1.0))  # cpu0 is ours.
        self.assertEqual(record['settings']['min_mhz'], 406)
        self.assertAlmostEqual(record['cpu']['busy_s'], 10.0, places=3)  # 2 s of CPU per 2 s, over 10 s.
        self.assertEqual(self.rows('SELECT mhz_active, active_n, min_mhz FROM records'), [(3000.0, 1.0, 406.0)])
        self.assertEqual(self.rows('SELECT ghz_active, purity FROM minutes'), [(3.0, 1.0)])

    def test_counters_are_rated_over_their_own_snapshots(self):
        # 12 cores flat out. The record's counter snapshot is read 0.5 s after the
        # record's end (a slow scan): 10.5 s of CPU time against dt = 10 s.
        delay = [0.0]

        def clock():
            at = self.uptime[0] + delay[0]
            stat(self.proc, round((at - 1000) * 12 * TICKS))  # What /proc/stat holds by then.
            return at
        monitor = Monitor(self.log, sys=self.sys, proc=self.proc, db=self.db, clock=clock)
        self.tick(monitor)
        for _ in range(4):
            self.tick(monitor)
        delay[0] = 0.5
        self.tick(monitor)
        monitor.close()
        busy, dt = self.rows('SELECT busy_s, dt FROM records')[0]
        self.assertEqual(dt, 10.0)
        self.assertAlmostEqual(busy / dt, 12.0, places=3)  # Not 12.6.

    def test_a_suspend_while_recording_drops_the_record_and_reports_the_gap_with_pre_suspend_battery(self):
        monitor = self.monitor()
        for _ in range(6):
            self.tick(monitor)  # One record, battery at 50 %.
        for _ in range(4):
            self.tick(monitor)
        # The collector read the clocks, then the machine slept during the record.
        put(self.sys / 'class/power_supply/BAT1/capacity', '40')
        real = self.uptime[0] + 2
        monitor.clock = lambda: real + 3600
        self.assertIsNotNone(monitor.update(real, self.wall + real - 1000, {}))
        monitor.clock = lambda: self.uptime[0]
        self.uptime[0] = real + 3600
        self.asleep[0] += 3600
        for _ in range(6):
            self.tick(monitor)
        monitor.close()
        records = self.rows('SELECT t0, t1, dt, pct FROM records ORDER BY t1')
        self.assertEqual(len(records), 2)
        for t0, t1, dt, _ in records:
            self.assertAlmostEqual(t1 - t0, dt, delta=0.01)  # None spans the suspend.
        (gap,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE event = 'gap'")]
        self.assertEqual((gap['bat_before']['pct'], gap['bat_after']['pct']), (50.0, 40.0))
        self.assertAlmostEqual(gap['t0'], records[0][1], delta=0.01)  # From the last record kept.
        self.assertGreater(gap['gap_s'], 3600)
        self.assertEqual(gap['suspended_s'], 3600.0)  # Asleep but for the 2 s before the record.

    def test_a_gap_says_how_much_of_it_was_a_suspend(self):
        # report.sleep_drain keeps a suspend's drain and leaves out a gap the collector was stalled through awake.
        charge = self.sys / 'class/power_supply/BAT1/charge_now'
        monitor = self.monitor()
        for asleep, now in ((1800, '1950000'), (0, '1900000'), (1000, '1850000')):
            for _ in range(6):
                self.tick(monitor)
            put(charge, now)  # 50 mAh over each half-hour gap: 1.55 W.
            self.asleep[0] += asleep
            self.tick(monitor, 1800)
        monitor.close()
        gaps = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE event = 'gap' ORDER BY t1")]
        self.assertEqual([(g['gap_s'], g['suspended_s']) for g in gaps],
                         [(1800.0, 1800.0), (1800.0, 0.0), (1800.0, 1000.0)])
        import report
        (kept,) = report.sleep_drain(gaps)
        self.assertEqual(kept['end'], gaps[0]['t1'])
        self.assertAlmostEqual(kept['watts'], 0.05 * 15.5 / 0.5, places=3)

    def test_without_cpufreq_records_have_no_clock(self):
        for path in self.sys.glob('devices/system/cpu/cpu*/cpufreq/scaling_cur_freq'):
            path.unlink()
        monitor = self.monitor()
        for _ in range(6):
            self.tick(monitor, busy=0)
        monitor.close()
        self.assertEqual(self.rows('SELECT mhz, mhz_active, active_n, busy_s FROM records'), [(None, None, None, 0.0)])
        self.assertEqual(self.rows('SELECT ghz, ghz_active, load, busy FROM minutes'), [(None, None, None, 0.0)])

    def test_a_broken_archive_and_database_never_stop_sampling(self):
        if power.zstd is None:
            self.skipTest('no zstd')
        put(self.log / '2000-01-01.jsonl.zst', 'corrupt')
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(f'PRAGMA user_version = {store.VERSION + 1}')  # A newer build's layout.
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch('power.stderr', new_callable=io.StringIO) as log_err:
            monitor = self.monitor()
            for _ in range(6):
                self.tick(monitor)
        self.assertIn('2000-01-01.jsonl.zst', log_err.getvalue())
        self.assertIn('only knows up to', log_err.getvalue() + err.getvalue())
        monitor.close()
        self.assertEqual(len(monitor.log.recent(0)), 1)  # The archive still has the record.

    def test_maintain_reads_back_what_a_failed_write_missed(self):
        monitor = self.monitor()
        real = store.insert
        with mock.patch('store.insert', side_effect=sqlite3.OperationalError('disk I/O error')), \
                contextlib.redirect_stderr(io.StringIO()):
            for _ in range(6):
                self.tick(monitor)
        self.assertEqual(self.rows('SELECT count(*) FROM records'), [(0,)])
        for _ in range(5):
            self.tick(monitor)  # Writes work again.
        while monitor.maintain():
            pass
        monitor.close()
        self.assertEqual(self.rows('SELECT count(*) FROM records'), [(2,)])
        self.assertIs(store.insert, real)

if __name__ == '__main__':
    unittest.main()
