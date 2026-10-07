import json
import random
import tempfile
import time
import unittest
from pathlib import Path
from power import (PRIOR, Deltas, GpuClients, PowerLog, attribute, battery_watts, experiment_assignment,
                   interrupts, load_attribution, log_footer, parse_fdinfo, settings, summarize, TICKS)


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

    def test_attribution_splits_chip_by_activity_and_platform_by_model(self):
        params = dict(PRIOR)   # floor 6 W, 0.2 W/(core·GHz²), ×1.3 per chip watt
        record = {'dt': 10, 'status': 'Discharging', 'bat': {'w': 20.0}, 'soc': {'w': 10.0},
                  'cpu': {'busy_s': 10.0, 'mhz': 2000},
                  'apps': {'Browser': {'cpu': 6.0}, 'Game': {'cpu': 2.0, 'gpu': 2.0}},
                  'display': {'bl': .5}, 'net': {'wlan0': {'rx': 0, 'tx': 0}}}
        rows = attribute(record, params)
        self.assertAlmostEqual(rows['Processor baseline'], 78.0)        # 1.3 × 6 W × 10 s
        self.assertAlmostEqual(rows['Kernel'] / rows['Browser'], 1 / 3)  # Unowned busy CPU
        self.assertGreater(rows['Game'], rows['Browser'])               # GPU time weighs more than CPU
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


if __name__ == '__main__':
    unittest.main()
