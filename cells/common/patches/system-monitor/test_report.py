import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import report


def record(t1, bat=15.0, soc=8.0, busy=2.0, mhz=2000, apps=None, exp=None, status='Discharging', bl=.5):
    r = {'v': 1, 't0': t1 - 10, 't1': t1, 'dt': 10, 'status': status, 'bat': {'w': bat, 'pct': 50, 'charge': 2e6,
         'volts': 15.5, 'unit': 'uAh'}, 'soc': {'w': soc}, 'cpu': {'busy_s': busy * 10, 'mhz': mhz},
         'gpu': {'gpu': 0, 'video': 0}, 'apps': apps or {}, 'display': {'bl': bl}, 'net': {}, 'disk': {},
         'settings': {}}
    if exp:
        r['exp'] = exp
    return r


class ReportTests(unittest.TestCase):
    def test_minute_means_app_load_and_experiment_washout(self):
        exp = {'run': 'r', 'name': 'aspm', 'block': 1, 'arm': 'B', 'value': 'x', 'washout': False}
        minutes, _ = report.aggregate([
            record(60 * 100 + 10, bat=10, apps={'Floorp': {'cpu': 5.0}}, exp=dict(exp, washout=True)),
            record(60 * 100 + 20, bat=20, apps={'Floorp': {'cpu': 5.0}}, exp=exp)])
        self.assertEqual(len(minutes), 1)
        m = minutes[0]
        self.assertAlmostEqual(m['bat'], 15)
        self.assertAlmostEqual(m['load'], 2 * 4)                  # busy cores × GHz²
        self.assertAlmostEqual(m['apps']['Floorp'][0], 10 * 4 / 20)  # cpu·GHz² per second
        self.assertTrue(m['exp'][4])                              # Any washout taints the minute

    def test_fit_recovers_coefficients(self):
        rng = np.random.default_rng(1)
        rows = []
        for i in range(400):
            load, bl = rng.uniform(1, 30), rng.uniform(.1, 1)
            soc = 5 + .2 * load + rng.normal(0, .1)
            rows.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'soc': soc, 'bl': bl,
                         'bat': .5 + 1.4 * soc + 3 * bl + rng.normal(0, .1)})
        model = report.fit_rows(report.battery_rows(rows))
        self.assertAlmostEqual(model['floor'], 5, delta=.2)
        self.assertAlmostEqual(model['load'], .2, delta=.01)
        self.assertAlmostEqual(model['chip'], 1.4, delta=.05)
        self.assertAlmostEqual(model['bl'], 3, delta=.2)
        self.assertAlmostEqual(model['usb'], report.PRIOR['usb'], delta=.01)  # Never varied → prior

    def test_attribution_sums_to_battery_power(self):
        model = dict(report.PRIOR, floor=5, load=.2, chip=1.4, bl=3)
        m = {'t': 0, 'dt': 60, 'st': 'D', 'bat': 20.0, 'soc': 8.0, 'load': 10.0, 'bl': .5,
             'apps': {'Floorp': [6.0, 0, 0], 'openroad': [2.0, 0, 0]}}
        parts = report.attribute(m, model)
        self.assertAlmostEqual(sum(parts['groups'].values()), 20.0)
        self.assertAlmostEqual(parts['groups']['Processor baseline'], 7.0)
        self.assertAlmostEqual(parts['apps']['Floorp'] / parts['apps']['openroad'], 3)
        self.assertAlmostEqual(parts['groups']['Display'], 1.5)

    def test_runtime_gain(self):
        self.assertAlmostEqual(report.runtime_gain(48, 16, 2), 60 * 48 * (1 / 14 - 1 / 16))
        self.assertEqual(report.runtime_gain(48, 16, 0), 0)
        self.assertLess(report.runtime_gain(48, 16, -1), 0)

    def test_what_if_ranks_and_brackets(self):
        rng = np.random.default_rng(2)
        rows = []
        for i in range(300):
            heavy = rng.uniform(0, 20)
            load = 2 + heavy
            soc = 5 + .2 * load
            bl = rng.uniform(.3, 1)
            rows.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'soc': soc, 'bl': bl,
                         'apps': {'openroad': [heavy, 0, 0], 'Floorp': [1.0, 0, 0]},
                         'bat': .5 + 1.4 * soc + 3 * bl + rng.normal(0, .2)})
        estimates, info = report.what_if(rows, 48, replicates=50)
        by_name = {e['name']: e for e in estimates}
        self.assertGreater(by_name['Close openroad']['minutes'], by_name['Close Floorp']['minutes'])
        for e in estimates:
            self.assertLessEqual(e['low'], e['minutes'] + 1e-6)
            self.assertGreaterEqual(e['high'], e['minutes'] - 1e-6)
        self.assertAlmostEqual(info['runtime_h'], 48 / info['power'])

    def test_experiment_effect_from_paired_blocks(self):
        minutes = []
        for block in range(1, 9):
            arm = 'A' if block % 4 in (1, 0) else 'B'   # A B B A A B B A
            for i in range(3):
                minutes.append({'t': block * 1000 + i * 60, 'dt': 60, 'st': 'D', 'bat': 16 if arm == 'A' else 14,
                                'exp': ['r1', 'aspm', block, arm, False, 'powersupersave' if arm == 'B' else 'performance']})
        minutes.append({'t': 99, 'dt': 60, 'st': 'D', 'bat': 99,
                        'exp': ['r1', 'aspm', 1, 'A', True, 'performance']})  # Washout: ignored
        effects = report.experiment_effects(minutes, 48, 16)
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertEqual((effect['pairs'], effect['value']), (4, 'powersupersave'))
        self.assertAlmostEqual(effect['watts'], 2)
        self.assertAlmostEqual(effect['minutes'], report.runtime_gain(48, 16, 2))

    def test_sleep_drain(self):
        events = [{'event': 'gap', 't1': 7200, 'gap_s': 3600,
                   'bat_before': {'pct': 60, 'charge': 2_000_000, 'volts': 15.5, 'unit': 'uAh'},
                   'bat_after': {'pct': 58, 'charge': 1_940_000}},
                  {'event': 'gap', 't1': 9000, 'gap_s': 60, 'bat_before': {'charge': 1}, 'bat_after': {'charge': 0}},
                  {'event': 'start', 't1': 1}]
        (sleep,) = report.sleep_drain(events)
        self.assertAlmostEqual(sleep['watts'], .06 * 15.5)
        self.assertAlmostEqual(sleep['pct_per_hour'], 2)

    def test_incremental_day_cache_matches_full_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, cache = Path(tmp) / 'log', Path(tmp) / 'cache'
            log.mkdir()
            now = time.time()
            day = time.strftime('%Y-%m-%d', time.localtime(now))
            base = int(time.mktime(time.strptime(day, '%Y-%m-%d'))) + 3600
            records = [record(base + 10 * i, bat=10 + i % 7) for i in range(40)]
            path = log / f'{day}.jsonl'
            path.write_text(''.join(json.dumps(r) + '\n' for r in records[:23]))
            report.load_day(day, log, cache)
            with open(path, 'a') as handle:
                handle.write(''.join(json.dumps(r) + '\n' for r in records[23:]))
            incremental, _ = report.load_day(day, log, cache)
            full, _ = report.aggregate(records)
            self.assertEqual(incremental, full)


if __name__ == '__main__':
    unittest.main()
