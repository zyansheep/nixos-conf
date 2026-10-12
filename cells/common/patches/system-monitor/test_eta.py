import math
import time
import json
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

    def test_recommendations_for_the_battery_menu(self):
        from test_report import factorial_minutes
        minutes = factorial_minutes(blocks=120)
        for m in minutes:
            m.update(pct=50, volts=15.5, charge=2e6, unit='uAh')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recommend.json'
            eta.write_recommendations(minutes, [], path)
            written = __import__('json').loads(path.read_text())
        self.assertEqual(set(written), {'updated', 'battery', 'comfort'})
        self.assertEqual(set(written['battery']), set(eta.RECOMMEND_KEYS))
        # Every setting saves here, so Battery turns on whichever savings are off now.
        self.assertTrue(written['battery']['apply'])
        self.assertTrue(all(a.endswith('=saving') for a in written['battery']['apply']))
        self.assertEqual(written['comfort']['change'], 'same as Battery until a 👍')

    def test_the_cutoff_comes_from_the_boots_that_died_on_battery(self):
        def boot(name, start, charges, st='D', pct=None):
            return [{'t': start + 60 * i, 'dt': 60, 'st': st, 'boot': name, 'charge': q * 1000,
                     'pct': pct if pct is not None else q / 31.25} for i, q in enumerate(charges)]
        minutes = (boot('died', 0, [93, 81, 69, 57])                   # 12 mAh a minute: 0.2 mAh/s.
                   + boot('by-hand', 1000, [1700, 1690], pct=54)      # Shut down at 54%.
                   + boot('slept', 2000, [300, 290], pct=9)            # Died later, while asleep.
                   + boot('running', 9000, [60, 50]))                 # This boot: not dead yet.
        fitted = eta.fit_cutoff(minutes)
        self.assertEqual(fitted['deaths'], 1)
        self.assertAlmostEqual(fitted['cutoff'], 0.057 - 5 * 0.0002, places=5)
        timed = {'boot': 'timed', 'cut': 0.0550, 't': 500, 'source': 'black box'}
        both = eta.fit_cutoff(minutes, [timed])
        self.assertEqual(both['deaths'], 2)
        self.assertAlmostEqual(both['cutoff'], np.median([0.056, 0.0550]), places=5)
        self.assertEqual(eta.fit_cutoff([])['cutoff'], 0.0)  # Nothing died yet: empty is zero charge.
        # battery-eta powered 'died' off itself: it looks like a death in the records, but isn't one.
        self.assertEqual(eta.fit_cutoff(minutes, [timed, {'boot': 'died', 'poweroff': True}])['deaths'], 1)
        self.assertEqual([d['boot'] for d in eta.deaths_in(minutes, running='died')], ['running'])
        with tempfile.TemporaryDirectory() as tmp:  # Remembered past the window.
            path = Path(tmp) / 'deaths.json'
            eta.remember_deaths(minutes, path=path)
            self.assertEqual(eta.fit_cutoff([], eta.load_gasps(path))['deaths'], 1)

    def test_a_last_gasp_near_the_cutoff_from_another_boot_is_a_death(self):
        with tempfile.TemporaryDirectory() as tmp:
            gasp, deaths = Path(tmp) / 'lastgasp.json', Path(tmp) / 'deaths.json'
            write = lambda **g: gasp.write_text(json.dumps({'boot': 'old', 'charge': 0.056, 'full': 3.125,
                                                            't': 1.0, 'seconds': 6.0, **g}))
            write(boot='now')
            self.assertIsNone(eta.harvest_gasp('now', gasp, deaths))
            self.assertTrue(gasp.exists())                            # Still this boot's.
            write(seconds=900, charge=0.12)                            # Shut down by hand at 4%.
            self.assertIsNone(eta.harvest_gasp('now', gasp, deaths))
            self.assertFalse(gasp.exists() or deaths.exists())
            write(rate=0.0002)
            death = eta.harvest_gasp('now', gasp, deaths)
            self.assertAlmostEqual(death['cut'], 0.0558)                # Cut within the 2 s after it.
            self.assertEqual([g['boot'] for g in eta.load_gasps(deaths)], ['old'])
            write(boot='older', poweroff=True)                          # battery-eta powered it off.
            self.assertTrue(eta.harvest_gasp('now', gasp, deaths)['poweroff'])
            self.assertEqual(eta.fit_cutoff([], eta.load_gasps(deaths))['deaths'], 1)

    def test_energy_counts_down_to_the_cutoff(self):
        params = dict(self.params, volts=[14.0] * 101, cutoff=0.056)
        self.assertAlmostEqual(eta.usable_energy(0.556, 3.125, params), 0.5 * 14.0, places=3)
        self.assertEqual(eta.usable_energy(0.04, 3.125, params), 0.0)

    def test_the_last_minutes_count_down_to_the_second_and_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            battery = root / 'class/power_supply/BAT1'
            battery.mkdir(parents=True)
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text('cc72fcaf-0000\n')
            values = {'type': 'Battery', 'status': 'Discharging', 'current_now': '820000', 'voltage_now': '13800000',
                      'charge_now': '80000', 'charge_full': '3125000', 'capacity': '3'}
            for name, value in values.items():
                (battery / name).write_text(value)
            notes = []
            live = eta.Live(root, proc=root, gasp=root / 'lastgasp.json', notify=lambda *note: notes.append(note))
            live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018)
            now = time.time()
            # Two minutes of the gauge falling 0.2 mAh/s, to 80 mAh now: 120 s to the cutoff.
            for i in range(60, 0, -1):
                live.samples.append((now - 2 * i, 11.3, 'Discharging', 0.080 + 0.0002 * 2 * i))
            out = live.estimate()
            detail = out['detail']
            self.assertIn('critical', out['class'])
            self.assertAlmostEqual(detail['cutout'] - time.time(), 120, delta=3)
            self.assertAlmostEqual(detail['cutout_pm'], 18, delta=1)
            self.assertRegex(out['text'], r'</span> [12]m \d\ds <span[^>]*>±18s</span>')
            self.assertEqual(len(notes), 1)                           # Within 5 minutes; once.
            self.assertTrue(notes[0][0].startswith('Battery cuts out at '))
            live.estimate()
            self.assertEqual(len(notes), 1)
            gasp = json.loads((root / 'lastgasp.json').read_text())   # The black box, below 5%.
            self.assertEqual((gasp['boot'], gasp['charge']), ('cc72fcaf', 0.08))
            (battery / 'status').write_text('Charging')
            live.estimate()
            self.assertFalse((root / 'lastgasp.json').exists())       # Plugged in: not a death.

    def test_once_a_death_taught_the_cutoff_it_powers_off_cleanly_before_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            battery = root / 'class/power_supply/BAT1'
            battery.mkdir(parents=True)
            (root / 'sys/kernel/random').mkdir(parents=True)
            (root / 'sys/kernel/random/boot_id').write_text('cc72fcaf-0000\n')
            values = {'type': 'Battery', 'status': 'Discharging', 'current_now': '820000', 'voltage_now': '13800000',
                      'charge_now': '62000', 'charge_full': '3125000', 'capacity': '2'}
            for name, value in values.items():
                (battery / name).write_text(value)
            notes, offs = [], []
            live = eta.Live(root, proc=root, gasp=root / 'lastgasp.json', notify=lambda *n: notes.append(n),
                            poweroff=lambda: offs.append(time.time()))
            live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018, deaths=5)
            now = time.time()
            for i in range(60, 0, -1):  # 0.2 mAh/s, to 62 mAh: 30 s to the cutoff.
                live.samples.append((now - 2 * i, 11.3, 'Discharging', 0.062 + 0.0002 * 2 * i))
            out = live.estimate()
            self.assertIn('\uf011', out['text'])                      # Counting down to the power-off …
            self.assertAlmostEqual(out['detail']['poweroff'] - time.time(), 0, delta=3)
            self.assertEqual(offs, [])                                # … but once is not enough.
            live.estimate()
            self.assertEqual(len(offs), 1)
            self.assertTrue(json.loads((root / 'lastgasp.json').read_text())['poweroff'])
            live.estimate()
            self.assertEqual(len(offs), 1)                            # Only once.
            live.params['deaths'] = 0                                  # Nothing learned: no power-off.
            live.clear_gasp()
            live.estimate()
            live.estimate()
            self.assertEqual(len(offs), 1)

    def test_countdown_format(self):
        self.assertEqual((eta.countdown(247.4), eta.countdown(41), eta.countdown(-2)), ('4m 07s', '41s', '0s'))

    def test_plus_minus_is_half_the_interval(self):
        self.assertIn('±0:15', eta.plus_minus(1.0, 1.5))

    def test_clock_format(self):
        self.assertEqual(eta.clock(1.5), '1:30')
        self.assertEqual(eta.clock(0.01), '0:01')
        self.assertEqual(eta.clock(float('inf')), '--')


if __name__ == '__main__':
    unittest.main()
