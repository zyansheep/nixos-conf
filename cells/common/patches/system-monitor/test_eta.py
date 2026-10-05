import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

import eta


class EtaTests(unittest.TestCase):
    params = dict(eta.DEFAULTS, mean=16.0, var=16.0, tau=15.0, observed=1e6, kappa=1.0, floor=5.0)

    def test_energy_follows_the_voltage_curve(self):
        volts = [14.0 + 3.0 * p / 100 for p in range(101)]
        full = eta.remaining_energy(1.0, 3.0, volts)
        half = eta.remaining_energy(0.5, 3.0, volts)
        self.assertLess(half / full, 0.5)   # The lower half holds less energy than the upper half.
        self.assertAlmostEqual(eta.remaining_energy(0.5, 3.0, None), 0.5 * 3.0 * 15.4)

    def test_horizon_draw_moves_from_recent_to_long_run_mean(self):
        short = eta.horizon_draw(self.params, 30.0, 0.1)
        long = eta.horizon_draw(self.params, 30.0, 10000)
        self.assertAlmostEqual(short[0], 30.0, delta=0.5)
        self.assertAlmostEqual(long[0], 16.0, delta=0.5)
        for median, low, high in (short, long, eta.horizon_draw(self.params, 30.0, 60)):
            self.assertLess(low, median)
            self.assertLess(median, high)
        # Averaging over a longer horizon smooths the draw: the interval narrows.
        width = lambda h: (lambda m, lo, hi: hi - lo)(*eta.horizon_draw(self.params, 16.0, h))
        self.assertLess(width(1000), width(60))

    def test_time_to_empty_brackets_and_scales_with_energy(self):
        median, low, high = eta.time_to_empty(self.params, 32.0, 16.0)
        self.assertAlmostEqual(median, 2.0, delta=0.1)          # 32 Wh at ~16 W
        self.assertLess(low, median)
        self.assertLess(median, high)
        self.assertGreater(eta.time_to_empty(self.params, 16.0, 16.0)[0] * 2, median * 0.95)
        heavy = eta.time_to_empty(dict(self.params, kappa=1.05), 32.0, 16.0)[0]
        self.assertLess(heavy, median)                           # Counter-calibrated draw is larger.

    def test_time_to_full_integrates_the_curve_and_respects_the_limit(self):
        params = dict(self.params, charge_curve=[1.0] * 80 + [0.5] * 21, charge_sigma=0.2)
        median, low, high = eta.time_to_full(params, 70.0, 90, None)
        self.assertAlmostEqual(median, 10 + 20)                  # 10 % at 1 %/min, 10 % at 0.5 %/min
        self.assertLess(low, median)
        self.assertGreater(high, median)
        self.assertAlmostEqual(eta.time_to_full(params, 70.0, 90, 2.0)[0], 15)   # Charging twice as fast
        self.assertEqual(eta.time_to_full(params, 89.7, 90, None), (0.0, 0.0, 0.0))

    def test_ou_fit_recovers_time_constant(self):
        rng = np.random.default_rng(3)
        rows, t, x = [], 0, 16.0
        for _ in range(4000):
            x = 16 + (x - 16) * math.exp(-1 / 10) + rng.normal(0, 2)
            rows.append({'t': t, 'bat': x, 'st': 'D', 'dt': 60})
            t += 60
        fitted = eta.fit_ou(rows)
        self.assertAlmostEqual(fitted['tau'], 10, delta=2)
        self.assertAlmostEqual(fitted['mean'], 16, delta=0.5)

    def test_live_label_and_energy_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery = Path(tmp) / 'class/power_supply/BAT1'
            battery.mkdir(parents=True)
            values = {'type': 'Battery', 'status': 'Discharging', 'current_now': '1000000', 'voltage_now': '15000000',
                      'charge_now': '1000000', 'charge_full': '3000000', 'charge_control_end_threshold': '90',
                      'capacity': '33'}
            for name, value in values.items():
                (battery / name).write_text(value)
            live = eta.Live(Path(tmp))
            live.params = dict(self.params, volts=[14.0 + 3.0 * p / 100 for p in range(101)])
            out = live.estimate()
            self.assertIn('discharging', out['class'])
            self.assertNotIn('sleep', out['detail'])                 # No suspends fitted yet
            live.params['sleep'] = (math.log(1.0), 0.2, 5)           # ~1 W asleep
            asleep = live.estimate()['detail']['sleep']
            self.assertAlmostEqual(asleep['hours'], live.estimate()['detail']['energy_wh'], delta=0.01)
            fill = int(next(c for c in out['class'] if c.startswith('fill'))[4:])
            self.assertLess(fill, 33)                 # Energy fraction is below the charge fraction here.
            self.assertRegex(out['text'], r'</span> \d+:\d\d <span[^>]*>±\d+:\d\d</span> <span[^>]*>15\.0W</span>$')
            (battery / 'status').write_text('Not charging')
            plugged = live.estimate()
            self.assertIn('plugged', plugged['class'])
            self.assertIn('\uf1e6', plugged['text'])

    def test_distribution_agrees_with_the_quantiles(self):
        median, low, high = eta.time_to_empty(self.params, 32.0, 20.0)
        times, pdf, cdf = eta.empty_distribution(self.params, 32.0, 20.0, low, high, points=400)
        at = lambda target: float(np.interp(target, times, cdf))
        self.assertAlmostEqual(at(median), 0.5, delta=0.02)
        self.assertAlmostEqual(at(low), 0.1, delta=0.02)
        self.assertAlmostEqual(at(high), 0.9, delta=0.02)
        self.assertAlmostEqual(float(np.trapezoid(pdf, times)), cdf[-1] - cdf[0], delta=0.02)
        times, pdf, cdf = eta.full_distribution(1.0, 0.25, 0.73, 1.38, points=400)
        self.assertAlmostEqual(float(np.interp(1.0, times, cdf)), 0.5, delta=0.01)

    def test_use_per_minute_of_charging(self):
        median, low, high = eta.use_per_charge(self.params, 32.0, 48.0)   # 32 W in, ~16 W typical draw
        self.assertAlmostEqual(median, 2.0, delta=0.1)
        self.assertLess(low, median)
        self.assertLess(median, high)
        with tempfile.TemporaryDirectory() as tmp:
            battery = Path(tmp) / 'class/power_supply/BAT1'
            battery.mkdir(parents=True)
            for name, value in {'type': 'Battery', 'status': 'Charging', 'current_now': '2000000',
                                'voltage_now': '16000000', 'charge_now': '1500000', 'charge_full': '3000000',
                                'charge_control_end_threshold': '90', 'capacity': '50'}.items():
                (battery / name).write_text(value)
            live = eta.Live(Path(tmp))
            live.params = dict(self.params)
            out = live.estimate()
            self.assertRegex(out['text'], r'~</span>\d+:\d\d <span[^>]*>full ~\d+:\d\d</span> <span[^>]*>32W</span>$')
            self.assertIn('', out['text'])                     # The bolt glyph is really there
            banked = out['detail']['banked']
            self.assertAlmostEqual(banked, eta.time_to_empty(live.params, out['detail']['energy_wh'], 16.0)[0])
            self.assertIn('charging', out['class'])
            self.assertAlmostEqual(out['detail']['ratio'], 2.0, delta=0.1)

    def test_train_writes_every_model_and_loads_fresh_only(self):
        rng = np.random.default_rng(7)
        minutes = []
        for i in range(200):
            load = rng.uniform(1, 20)
            soc = 6 + .2 * load
            minutes.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'soc': soc, 'bl': .5, 'pct': 80 - i * .2,
                            'volts': 16 - i * .005, 'charge': 2.5e6 - i * 1000, 'unit': 'uAh',
                            'bat': .5 + 1.4 * soc + 1.5 + rng.normal(0, .2), 'set': {}})
        params = eta.train(minutes, [], now=1000.0)
        self.assertAlmostEqual(params['attribution']['chip'], 1.4, delta=.1)
        self.assertGreater(params['tau'], 0)
        self.assertEqual(params['battery_minutes'], 200)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.json'
            eta.save_models(params, path)
            self.assertIsNone(eta.load_models(path))                       # Trained at t=1000: stale
            eta.save_models(dict(params, fitted=__import__('time').time()), path)
            self.assertAlmostEqual(eta.load_models(path)['attribution']['chip'], params['attribution']['chip'])

    def test_plus_minus_is_half_the_interval(self):
        self.assertIn('±0:15', eta.plus_minus(1.0, 1.5))

    def test_clock_format(self):
        self.assertEqual(eta.clock(1.5), '1:30')
        self.assertEqual(eta.clock(0.01), '0:01')
        self.assertEqual(eta.clock(float('inf')), '--')


if __name__ == '__main__':
    unittest.main()
