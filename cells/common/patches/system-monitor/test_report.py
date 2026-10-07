import unittest

import numpy as np

import report
import store


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
        minutes, _ = store.aggregate([
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
        self.assertEqual((effect['a_watts'], effect['b_watts']), (16, 14))   # Frozen floor = B blocks

    def test_psr_flag_and_boot_effect(self):
        self.assertTrue(store.psr_enabled('0'))
        self.assertFalse(store.psr_enabled('16'))
        self.assertFalse(store.psr_enabled(None))          # Before logging: nixos-hardware's 0x10
        rng = np.random.default_rng(5)
        minutes, t = [], 0
        for boot in range(6):
            psr = boot % 2 == 1
            for _ in range(60):
                load = rng.uniform(1, 20)
                minutes.append({'t': t, 'dt': 60, 'st': 'D', 'load': load, 'soc': 5 + .2 * load, 'bl': .5,
                                'bat': 9 + .3 * load - (1.5 if psr else 0) + rng.normal(0, .5),
                                'boot': f'b{boot}', 'set': {'psr': psr}})
                t += 60
        effect = report.boot_effect(minutes, 48, 16, replicates=200)
        self.assertAlmostEqual(effect['watts'], 1.5, delta=.3)
        self.assertLess(effect['watts_low'], effect['watts'])
        self.assertGreater(effect['minutes'], 0)
        progress = report.boot_effect(minutes[:120], 48, 16)   # One boot per arm: not enough yet
        self.assertNotIn('watts', progress)
        self.assertEqual(progress['boots'], {'off': 1, 'on': 1})

    def test_sleep_drain(self):
        events = [{'event': 'gap', 't1': 7200, 'gap_s': 3600,
                   'bat_before': {'pct': 60, 'charge': 2_000_000, 'volts': 15.5, 'unit': 'uAh'},
                   'bat_after': {'pct': 58, 'charge': 1_940_000}},
                  {'event': 'gap', 't1': 9000, 'gap_s': 60, 'bat_before': {'charge': 1}, 'bat_after': {'charge': 0}},
                  {'event': 'start', 't1': 1}]
        (sleep,) = report.sleep_drain(events)
        self.assertAlmostEqual(sleep['watts'], .06 * 15.5)
        self.assertAlmostEqual(sleep['pct_per_hour'], 2)

    def test_sleep_runtime(self):
        sleeps = [{'watts': w, 'hours': h} for w, h in ((0.9, 3), (1.0, 1), (1.1, 2), (0.95, 8), (1.05, 0.5))]
        estimate = report.sleep_runtime(sleeps, 24.0)
        self.assertAlmostEqual(estimate['hours'], 24.0, delta=1.5)        # ~1 W from 24 Wh
        self.assertLess(estimate['low'], estimate['hours'])
        self.assertGreater(estimate['high'], estimate['hours'])
        self.assertIsNone(report.sleep_runtime(sleeps[:2], 24.0))       # Too few suspends
        noisy = report.sleep_runtime(sleeps + [{'watts': 3.0, 'hours': 1}], 24.0)
        self.assertGreater(noisy['high'] - noisy['low'], estimate['high'] - estimate['low'])


def factorial_minutes(blocks=480, seed=7):
    """Simulated factorial run over ASPM, APST and CPU boost with known effects."""
    rng = np.random.default_rng(seed)
    design = [dict(zip(('aspm', 'apst', 'boost'), combo)) for combo in np.ndindex(2, 2, 2)]
    minutes, t = [], 1_000_000 * 60
    for block in range(blocks):
        x = design[rng.integers(len(design))]
        drift = rng.normal(0, 1.5)  # Workload noise shared by a block's minutes.
        for minute in range(4):
            busy = max(0.2, rng.normal(2, 0.6))
            load = busy * (2.0 if x['boost'] else 4.0)  # Boost off: lower GHz², so lower load.
            bat = (12 + 0.8 * load - 2.0 * x['aspm'] - 0.5 * x['apst'] - 1.0 * x['aspm'] * x['apst']
                   + drift + rng.normal(0, 0.8))
            minutes.append({'t': t, 'dt': 60, 'st': 'D', 'bat': bat, 'soc': bat - 4, 'load': load, 'busy': busy,
                            'gpu': 0, 'video': 0, 'bl': .5, 'kbd': 0, 'wifi': 0, 'disk': 0, 'usb': 0, 'audio': 0,
                            'set': {'aspm': 'powersupersave' if x['aspm'] else 'performance',
                                    'apst': '100000' if x['apst'] else '0', 'boost': '0' if x['boost'] else '1',
                                    'hz': 60.0},
                            'exp': ['run', 'aspm+apst+boost', block, '?', minute == 0, None]})
            t += 60
    return minutes


class LeverTests(unittest.TestCase):
    def test_factorial_recovers_main_effects_interaction_and_total_cpu_effect(self):
        result = report.lever_effects(factorial_minutes(), energy=50, power=15, replicates=100)
        effects, now = {e['experiment']: e for e in result['effects']}, result['current']
        # Each setting's saving given the others as they are now (ASPM and APST save 1 W more together).
        self.assertAlmostEqual(effects['aspm']['watts'], 2.0 + 1.0 * now['apst'], delta=0.6)
        self.assertAlmostEqual(effects['apst']['watts'], 0.5 + 1.0 * now['aspm'], delta=0.6)
        # Boost off saves through lower load: 0.8 W/load × (4 − 2) × busy 2 ≈ 3.2 W in total.
        self.assertAlmostEqual(effects['boost']['watts'], 3.2, delta=0.6)
        (pair,) = [i for i in result['interactions'] if set(i['pair']) == {'aspm', 'apst'}]
        self.assertAlmostEqual(pair['watts'], 1.0, delta=0.5)
        self.assertLess(effects['aspm']['watts_low'], effects['aspm']['watts'])

    def test_best_combination_turns_on_what_saves(self):
        result = report.lever_effects(factorial_minutes(), energy=50, power=15, replicates=50)
        result['current'] = dict.fromkeys(result['current'], False)
        best = report.best_config(result, {'aspm', 'apst'}, energy=50, power=15)
        self.assertEqual(best['config'], {'aspm': True, 'apst': True})
        self.assertGreater(best['watts'], 2.5)

    def test_settings_read_both_value_formats_and_the_experiments_value_wins(self):
        logged = {'apst': '0', 'abm': 3, 'hz': 48.0, 'profile': 'low-power', 'wifi_ps': 'off,on'}
        states = report.lever_states(logged, 60.0, chosen={'apst': '100000', 'refresh': '2256x1504@59.999'})
        self.assertEqual((states['apst'], states['abm'], states['refresh'], states['profile'], states['wifi-ps']),
                         (True, True, False, True, False))
        self.assertTrue(report.saving('profile', 'power-saver') and report.saving('abm', '3'))

    def test_no_experiments_means_no_effects(self):
        minutes = [dict(m, exp=None) for m in factorial_minutes(blocks=20)]
        self.assertEqual(report.lever_effects(minutes, 50, 15)['effects'], [])


if __name__ == '__main__':
    unittest.main()
