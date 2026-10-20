import contextlib
import io
import json
import math
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import eta
import store

STATE = tempfile.TemporaryDirectory()
REAL = (eta.GASP, eta.DEATHS, eta.MODELS, eta.RUNTIME)


def setUpModule():
    # Nothing here may touch the real state: every default path points into a temporary directory.
    state = Path(STATE.name)
    eta.GASP, eta.DEATHS, eta.MODELS, eta.RUNTIME = (state / 'lastgasp.json', state / 'deaths.json',
                                                     state / 'models.json', state / 'run')


def tearDownModule():
    eta.GASP, eta.DEATHS, eta.MODELS, eta.RUNTIME = REAL
    STATE.cleanup()


class Clock:
    """Wall clock and boot-time clock, moved by hand."""

    def __init__(self):
        self.t, self.m = 1.8e9, 5000.0

    def wall(self):
        return self.t

    def mono(self):
        return self.m

    def tick(self, seconds=2.0):
        self.t += seconds
        self.m += seconds


def battery(root, **values):
    b = root / 'class/power_supply/BAT1'
    b.mkdir(parents=True, exist_ok=True)
    for name, value in {'type': 'Battery', **values}.items():
        (b / name).write_text(str(value))
    return b


def mains(root, online):
    ac = root / 'class/power_supply/ACAD'
    ac.mkdir(parents=True, exist_ok=True)
    (ac / 'type').write_text('Mains')
    (ac / 'online').write_text(str(int(online)))


def make_live(root, clock=None, **options):
    """A Live on a fake /sys and /proc, every path in `root`, the clocks fake."""
    (root / 'sys/kernel/random').mkdir(parents=True, exist_ok=True)
    (root / 'sys/kernel/random/boot_id').write_text('cc72fcaf-0000\n')
    clock = clock or Clock()
    options = {'notify': lambda *n: None, 'poweroff': lambda: True, **options}
    return eta.Live(root, proc=root, gasp=root / 'lastgasp.json', deaths=root / 'deaths.json',
                    models=root / 'models.json', db=root / 'power.sqlite3', runtime=root / 'run',
                    clock=clock.wall, monotonic=clock.mono, **options)


LOW = {'status': 'Discharging', 'current_now': 820000, 'voltage_now': 13800000, 'charge_full': 3125000, 'capacity': 2}


class EtaTests(unittest.TestCase):
    params = dict(eta.DEFAULTS, mean=16.0, level=math.log(16.0), var=0.06, tau=15.0, level_n=1e6, level_var=0.0,
                  kappa=1.0, floor=5.0)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_energy_follows_the_voltage_curve(self):
        volts = [14.0 + 3.0 * p / 100 for p in range(101)]
        full = eta.remaining_energy(1.0, 3.0, volts)
        half = eta.remaining_energy(0.5, 3.0, volts)
        self.assertLess(half / full, 0.5)   # The lower half holds less energy than the upper half.
        self.assertAlmostEqual(eta.remaining_energy(0.5, 3.0, None), 0.5 * 3.0 * 15.4)

    def test_horizon_draw_moves_from_recent_to_the_mean_draw(self):
        short = eta.horizon_draw(self.params, 30.0, 0.1)
        long = eta.horizon_draw(self.params, 30.0, 10000)
        self.assertAlmostEqual(short[0], 30.0, delta=0.5)
        # Over a long horizon the average draw is the mean, e^(level + var/2), not the median e^level.
        self.assertAlmostEqual(long[0], 16.0 * math.exp(0.03), delta=0.1)
        self.assertAlmostEqual(eta.typical_draw(self.params), 16.0 * math.exp(0.03))
        for median, low, high in (short, long, eta.horizon_draw(self.params, 30.0, 60)):
            self.assertLess(low, median)
            self.assertLess(median, high)
        # Averaging over a longer horizon smooths the draw: the interval narrows.
        width = lambda h: (lambda m, lo, hi: hi - lo)(*eta.horizon_draw(self.params, 16.0, h))
        self.assertLess(width(1000), width(60))

    def test_the_centre_is_the_median_of_the_simulated_average_draw(self):
        # Log draw an OU process (τ 15 min, variance 0.06) from 30 W around a 16 W level, in 6 s steps.
        rng, steps, tau, var = np.random.default_rng(5), 600, 15.0, 0.06
        a = math.exp(-0.1 / tau)
        z = np.full(4000, math.log(30.0 / 16.0))
        total = np.zeros(4000)
        for _ in range(steps):
            z = z * a + rng.normal(0, math.sqrt(var * (1 - a * a)), 4000)
            total += np.exp(math.log(16.0) + z)
        simulated = float(np.median(total / steps))   # The average draw over the next hour.
        median = eta.horizon_draw(self.params, 30.0, 60)[0]
        self.assertAlmostEqual(median / simulated, 1, delta=0.008)
        plain = math.exp(math.log(16) + math.log(30 / 16) * (1 - math.exp(-4)) / 4)   # Without the shift: ~3% low.
        self.assertLess(plain / simulated, 0.98)

    def test_tiny_horizons_never_give_a_negative_spread(self):
        params = dict(self.params, tau=240.0, level_var=0.0)
        for minutes in (1e-9, 1e-6, 1e-3, 0.01, 2.39, 2.41):
            mu, sigma = eta.draw_lognormal(params, 20.0, minutes)
            self.assertTrue(math.isfinite(mu) and sigma > 0, minutes)
        # The series below x = 0.01 meets the closed form above it.
        for below, above in zip(eta.ou_terms(0.00999999), eta.ou_terms(0.01)):
            self.assertAlmostEqual(below, above, delta=1e-7)

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
        rows, t, z = [], 0, math.log(16)
        for _ in range(4000):
            z = math.log(16) + (z - math.log(16)) * math.exp(-1 / 10) + rng.normal(0, 0.1)
            rows.append({'t': t, 'bat': math.exp(z), 'st': 'D', 'dt': 60})
            t += 60
        fitted = eta.fit_ou(rows)
        self.assertAlmostEqual(fitted['tau'], 10, delta=2)
        self.assertAlmostEqual(fitted['var'], 0.1 ** 2 / (1 - math.exp(-2 / 10)), delta=0.01)
        self.assertAlmostEqual(eta.typical_draw(fitted), 16, delta=1.5)
        # The level averages 300 contiguous minutes: ~var·2τ/300.
        self.assertAlmostEqual(fitted['level_var'], fitted['var'] * 2 * fitted['tau'] / 300, delta=fitted['var'] * 0.01)
        # With a single time constant the truth, its calibration factors are ~1 over half an hour and
        # more; above 1 a few minutes ahead, where the recent draw (a 10-minute mean) lags the state.
        params = dict(eta.DEFAULTS, **fitted)
        factor = dict(zip(eta.SCALE_MINUTES, eta.fit_scale(rows, params)['factor']))
        self.assertTrue(all(0.85 < factor[m] < 1.15 for m in (30, 60, 120, 240)), factor)
        self.assertGreater(factor[2], 1.3)

    def test_the_level_counts_runs_as_independent(self):
        var, tau = 0.08, 20.0
        self.assertAlmostEqual(eta.level_variance([1] * 300, var, tau), var / 300)       # Scattered minutes.
        one = eta.level_variance([300], var, tau)
        self.assertAlmostEqual(one, var * 2 * tau / 300, delta=one * 0.1)                # One long run.
        self.assertLess(eta.level_variance([30] * 10, var, tau), one)                     # Ten short runs: better known.

    def test_calibration_widens_short_horizons_when_minutes_are_noisier_than_one_time_constant(self):
        rng = np.random.default_rng(6)
        rows, z = [], 0.0
        for i in range(3000):
            z = z * math.exp(-1 / 30) + rng.normal(0, 0.08)
            rows.append({'t': 60 * i, 'bat': 15 * math.exp(z + rng.normal(0, 0.25)), 'st': 'D', 'dt': 60})
        params = dict(eta.DEFAULTS, **eta.fit_ou(rows))
        scale = eta.fit_scale(rows, params)
        self.assertEqual(scale['minutes'], list(eta.SCALE_MINUTES))
        factor = dict(zip(scale['minutes'], scale['factor']))
        self.assertGreater(factor[2], 1.3)                       # Minute noise the time constant can't see.
        self.assertGreater(factor[2], factor[60])
        params['scale'] = scale
        between = eta.horizon_scale(params, math.sqrt(2 * 5))    # Interpolated in log minutes.
        self.assertAlmostEqual(between, (factor[2] + factor[5]) / 2, places=6)
        self.assertEqual(eta.horizon_scale(params, 0.1), factor[2])
        self.assertEqual(eta.horizon_scale(params, 1000), factor[240])
        self.assertIsNone(eta.fit_scale(rows[:20], params))

    def test_the_level_follows_how_the_laptop_is_used_now(self):
        rng = np.random.default_rng(4)
        rows = [{'t': 60 * i, 'bat': (20 if i < 2000 else 10) * math.exp(rng.normal(0, 0.1)), 'st': 'D', 'dt': 60}
                for i in range(2600)]
        fitted = eta.fit_ou(rows)
        self.assertAlmostEqual(eta.typical_draw(fitted), 10, delta=0.3)   # The last 5 hours, not the month
        self.assertGreater(fitted['mean'], 17)
        self.assertAlmostEqual(eta.horizon_draw(dict(self.params, **fitted), 14.0, 10000)[0], 10, delta=0.3)

    def test_live_label_and_energy_fill(self):
        battery(self.root, status='Discharging', current_now=1000000, voltage_now=15000000, charge_now=1000000,
                charge_full=3000000, charge_control_end_threshold=90, capacity=33)
        live = make_live(self.root)
        live.params = dict(self.params, volts=[14.0 + 3.0 * p / 100 for p in range(101)])
        out = live.estimate()
        self.assertIn('discharging', out['class'])
        self.assertNotIn('sleep', out['detail'])                 # No suspends fitted yet
        live.params['sleep'] = (math.log(1.0), 0.2, 5)           # ~1 W asleep
        asleep = live.estimate()['detail']['sleep']
        self.assertAlmostEqual(asleep['hours'], live.estimate()['detail']['energy_wh'], delta=0.01)
        fill = int(next(c for c in out['class'] if c.startswith('fill'))[4:])
        self.assertLess(fill, 33)                 # Energy fraction is below the charge fraction here.
        self.assertRegex(out['text'], r'</span> \d+:\d\d <span[^>]*>±\d+:\d\d</span> <span[^>]*>15\.0W</span>$')
        shown = re.search(r'</span> (\d+):(\d\d) ', out['text'])     # eta.json's hours, as the menu reads them:
        self.assertAlmostEqual(out['detail']['hours'] * 60, int(shown[1]) * 60 + int(shown[2]), delta=1)  # the label's.
        self.assertLess(out['detail']['low'], out['detail']['hours'])
        self.assertLess(out['detail']['hours'], out['detail']['high'])
        (self.root / 'class/power_supply/BAT1/status').write_text('Not charging')
        plugged = live.estimate()
        self.assertIn('plugged', plugged['class'])
        self.assertIn('\uf1e6', plugged['text'])

    def test_unknown_status_on_battery_is_not_ac(self):
        b = battery(self.root, **dict(LOW, charge_now=80000, status='Unknown'))
        mains(self.root, False)
        live = make_live(self.root)
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056)
        (self.root / 'lastgasp.json').write_text('{}')
        out = live.estimate()
        self.assertNotIn('plugged', out['class'])                  # Not "on AC": the battery runs it.
        self.assertIn('discharging', out['class'])
        self.assertEqual(json.loads((self.root / 'lastgasp.json').read_text())['charge'], 0.08)   # Box kept.
        mains(self.root, True)
        self.assertIn('plugged', live.estimate()['class'])          # Unknown on mains is plugged.
        self.assertFalse((self.root / 'lastgasp.json').exists())
        mains(self.root, False)
        (b / 'status').write_text('Full')
        self.assertIn('plugged', live.estimate()['class'])

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
        median, low, high = eta.use_per_charge(self.params, 32.0, 48.0)   # 32 W in, ~16.5 W mean draw
        self.assertAlmostEqual(median, 32 / eta.typical_draw(self.params), delta=0.1)
        self.assertLess(low, median)
        self.assertLess(median, high)
        battery(self.root, status='Charging', current_now=2000000, voltage_now=16000000, charge_now=1500000,
                charge_full=3000000, charge_control_end_threshold=90, capacity=50)
        live = make_live(self.root)
        live.params = dict(self.params)
        out = live.estimate()
        self.assertRegex(out['text'], r'~</span>\d+:\d\d <span[^>]*>full ~\d+:\d\d</span> <span[^>]*>32W</span>$')
        self.assertIn('\uf0e7', out['text'])                     # The bolt glyph is really there
        banked = out['detail']['banked']
        self.assertAlmostEqual(banked, eta.time_to_empty(live.params, out['detail']['energy_wh'],
                                                         eta.typical_draw(live.params))[0])
        self.assertIn('charging', out['class'])
        self.assertAlmostEqual(out['detail']['ratio'], 32 / eta.typical_draw(self.params), delta=0.1)

    def test_train_writes_every_model_and_loads_fresh_only(self):
        rng = np.random.default_rng(7)
        minutes = []
        for i in range(200):
            load = rng.uniform(1, 20)
            soc = 6 + .2 * load
            minutes.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'ghz': 1.5, 'soc': soc, 'bl': .5, 'pct': 80 - i * .2,
                            'volts': 16 - i * .005, 'charge': 2.5e6 - i * 1000, 'unit': 'uAh',
                            'bat': .5 + 1.4 * soc + 1.5 + rng.normal(0, .2), 'set': {}})
        params = eta.train(minutes, [], now=1000.0, known=[])
        self.assertAlmostEqual(params['attribution']['chip'], 1.4, delta=.1)
        # report.fit_rows' flat attribution, its data health too, as models.json carries it.
        self.assertTrue({'rows', 'experiment_rows', 't0', 't1', 'kept', 'prior'} <= set(params['attribution']))
        self.assertTrue(all(isinstance(v, float) for v in params['attribution'].values()))
        self.assertEqual((params['attribution']['rows'], params['attribution']['prior']), (200.0, 0.0))
        self.assertGreater(params['tau'], 0)
        self.assertEqual(params['battery_minutes'], 200)
        self.assertAlmostEqual(params['typical'], math.exp(params['level'] + params['var'] / 2))
        rows = eta.report.battery_rows(minutes)  # The gains' draw: the window's at your settings.
        self.assertAlmostEqual(params['typical_power'], eta.report.typical_power(rows, params['attribution']))
        path = self.root / 'models.json'
        eta.save_models(params, path)
        self.assertIsNone(eta.load_models(path))                       # Trained at t=1000: stale
        eta.save_models(dict(params, fitted=time.time()), path)
        self.assertAlmostEqual(eta.load_models(path)['attribution']['chip'], params['attribution']['chip'])
        self.assertEqual(eta.load_models(path)['version'], eta.MODELS_VERSION)
        path.write_text(json.dumps(dict(params, version=3, fitted=time.time())))
        self.assertIsNone(eta.load_models(path))                       # Another version …
        self.assertEqual(eta.load_models(path, version=None)['version'], 3)   # … unless any will do.
        # Too few battery rows (a store migration underway): the last attribution (said so) and draw models stay,
        previous = dict(params, attribution={'chip': 9.0}, level=math.log(7.0))
        few = eta.train(minutes[:10], [], known=[], previous=previous)
        self.assertEqual((few['attribution']['chip'], few['attribution']['kept']), (9.0, 1.0))
        self.assertEqual(few['level'], math.log(7.0))
        self.assertEqual(eta.train(minutes[:10], [], known=[])['attribution']['prior'], 1.0)
        # but not one from before an update (models.json of another version): its terms meant other things.
        older = eta.train(minutes[:10], [], known=[], previous=dict(previous, version=eta.MODELS_VERSION - 1))
        self.assertEqual(older['attribution']['prior'], 1.0)
        # The sleep model sees the minutes (which gaps were on battery); fit_rows the last model and the minutes.
        with mock.patch.object(eta.report, 'sleep_drain', wraps=eta.report.sleep_drain) as drain, \
                mock.patch.object(eta.report, 'fit_rows', wraps=eta.report.fit_rows) as fit_rows:
            eta.train(minutes, [], known=[], previous=dict(params, version=eta.MODELS_VERSION))
        self.assertIs(drain.call_args.args[1], minutes)
        self.assertEqual(fit_rows.call_args.kwargs['previous'], params['attribution'])
        self.assertIs(fit_rows.call_args.kwargs['minutes'], minutes)

    def test_recommendations_for_the_battery_menu(self):
        from test_report import factorial_minutes
        minutes = factorial_minutes(blocks=120)
        for m in minutes:
            m.update(pct=50, volts=15.5, charge=2e6, unit='uAh')
        path = self.root / 'recommend.json'
        eta.write_recommendations(minutes, [], path, exclude=())
        written = json.loads(path.read_text())
        self.assertEqual(set(written), {'updated', 'battery', 'comfort'})
        self.assertEqual(set(written['battery']), set(eta.RECOMMEND_KEYS))
        # Every setting saves here, so Battery turns on whichever savings are off now.
        self.assertTrue(written['battery']['apply'])
        self.assertTrue(all(a.endswith('=saving') for a in written['battery']['apply']))
        self.assertEqual(written['comfort']['change'], 'same as Battery until a 👍')

    def test_recommendations_are_at_your_settings_and_leave_the_pool_alone(self):
        from test_report import factorial_minutes
        minutes = factorial_minutes(blocks=120)
        for m in minutes:
            m.update(pct=50, volts=15.5, charge=2e6, unit='uAh', ghz=1.5)
        # Then your own settings, outside experiments: none of the three saving.
        minutes += [dict(minutes[-1], t=minutes[-1]['t'] + 60 * (i + 1), exp=None,
                         set={'aspm': 'performance', 'apst': '0', 'boost': '1', 'hz': 60.0}) for i in range(5)]
        rows = eta.report.battery_rows(minutes)
        attribution = eta.report.fit_rows(rows, minutes=minutes)
        params = {'attribution': attribution, 'cutoff': 0.056, 'deaths': 5, 'volts': [13.0] * 101}
        with mock.patch.object(eta.report, 'full_energy', return_value=50.0) as full, \
                mock.patch.object(eta.report, 'recommendations', wraps=eta.report.recommendations) as recommend, \
                mock.patch.object(eta, 'unrecommended', return_value=('aspm',)):
            written = eta.write_recommendations(minutes, [], self.root / 'recommend.json', params=params)
        # The energy above the cutoff, at the draw of your settings by the model just trained (not the raw mean),
        self.assertAlmostEqual(full.call_args.kwargs['reserve_wh'], 0.056 * 13.0)
        power = recommend.call_args.args[3]
        self.assertEqual(power, eta.report.typical_power(rows, attribution))
        self.assertNotAlmostEqual(power, sum(m['bat'] for m in rows) / len(rows), places=2)
        # and what experiments may not touch, untouched.
        self.assertEqual(recommend.call_args.kwargs['exclude'], ('aspm',))
        self.assertTrue(written['battery']['apply'])
        self.assertFalse([a for a in written['battery']['apply'] if a.startswith('aspm=')])
        # Called without the models (power-experiment goal --apply): models.json's, if it is this version.
        eta.save_models(dict(params, fitted=1.0), eta.MODELS)
        with mock.patch.object(eta.report, 'fit_rows', side_effect=AssertionError('fitted again')), \
                mock.patch.object(eta.report, 'full_energy', return_value=50.0), \
                mock.patch.object(eta, 'unrecommended', return_value=()):
            self.assertTrue(eta.write_recommendations(minutes, [], self.root / 'again.json')['battery']['apply'])
        eta.MODELS.unlink()

    def test_recommendations_never_touch_what_experiments_may_not(self):
        import experiment
        self.assertEqual(set(eta.RISKY), set(experiment.RISKY))          # The copy for when it won't import.
        fake = types.SimpleNamespace(unrecommended=lambda: ('apst', 'aspm'), RISKY=('aspm', 'apst', 'gpu-dpm'),
                                     quarantined=lambda: {'wifi-ps': 'xHCI died'})
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.dict(sys.modules, {'experiment': fake}):
                self.assertEqual(eta.unrecommended(), ('apst', 'aspm'))
                fake.unrecommended = lambda: 1 / 0                        # It fails: the risky and quarantined ones,
                self.assertEqual(eta.unrecommended(), ('apst', 'aspm', 'gpu-dpm', 'wifi-ps'))
                fake.quarantined = lambda: 1 / 0                          # the risky ones at least,
                self.assertEqual(eta.unrecommended(), ('apst', 'aspm', 'gpu-dpm'))
            with mock.patch.dict(sys.modules, {'experiment': None}):     # even if experiment.py won't import.
                self.assertEqual(eta.unrecommended(), eta.RISKY)

    def test_the_reserve_below_the_cutoff(self):
        curve = [12.0 + p / 50 for p in range(101)]
        params = {'cutoff': 0.056, 'deaths': 5, 'volts': curve}
        battery(self.root, charge_full=3000000)
        # What usable_energy leaves out of a full charge, on the voltage curve.
        self.assertAlmostEqual(eta.reserve_wh(params, sys_root=self.root),
                               eta.remaining_energy(1.0, 3.0, curve) - eta.usable_energy(3.0, 3.0, params))
        empty = self.root / 'nothing'                                    # No /sys (the build): ~the same.
        self.assertAlmostEqual(eta.reserve_wh(params, sys_root=empty), eta.reserve_wh(params, sys_root=self.root),
                               delta=0.002)
        with mock.patch.object(eta, 'DEATHS', self.root / 'deaths.json'), \
                mock.patch.object(eta, 'MODELS', self.root / 'models.json'):
            self.assertEqual(eta.reserve_wh(sys_root=empty), 0.0)        # Nothing learned yet.
            eta.remember([{'boot': 'a', 't': 1, 'cut': 0.056}], eta.DEATHS)
            self.assertAlmostEqual(eta.reserve_wh(sys_root=empty), 0.056 * 15.4)   # deaths.json's, no curve.
            eta.save_models(dict(params, fitted=1.0), eta.MODELS)
            self.assertAlmostEqual(eta.reserve_wh(sys_root=self.root), eta.reserve_wh(params, sys_root=self.root))

    def test_a_refit_reads_no_apps_and_recommends_from_what_it_trained(self):
        live = make_live(self.root)
        loads = []

        def load(start, end, db=None, apps=True):
            loads.append(apps)
            return [], []
        with mock.patch.object(eta, 'db_ready', return_value=True), \
                mock.patch.object(eta.report, 'load_range', side_effect=load), \
                mock.patch.object(eta, 'write_recommendations') as recommend:
            self.assertTrue(live.refit())
        self.assertEqual(loads, [False])
        self.assertIs(recommend.call_args.kwargs['params'], live.params)
        self.assertEqual(json.loads((self.root / 'models.json').read_text())['version'], eta.MODELS_VERSION)

    @staticmethod
    def boots():
        def boot(name, start, charges, st='D', pct=None):
            return [{'t': start + 60 * i, 'dt': 60, 'st': st, 'boot': name, 'charge': q * 1000,
                     'pct': pct if pct is not None else q / 31.25} for i, q in enumerate(charges)]
        return (boot('died', 0, [93, 81, 69, 57])                   # 12 mAh a minute: 0.2 mAh/s.
                + boot('by-hand', 1000, [1700, 1690], pct=54)      # Shut down at 54%.
                + boot('slept', 2000, [300, 290], pct=9)            # Died later, while asleep.
                + boot('running', 9000, [60, 50]))                 # This boot: not dead yet.

    def test_the_cutoff_comes_from_the_boots_that_died_on_battery(self):
        minutes = self.boots()
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
        path = self.root / 'deaths.json'  # Remembered past the window.
        eta.remember_deaths(minutes, path=path)
        self.assertEqual(eta.fit_cutoff([], eta.load_gasps(path))['deaths'], 1)

    def test_only_cuts_the_journal_confirms_teach_the_cutoff(self):
        minutes = self.boots() + [{'t': 3000 + 60 * i, 'dt': 60, 'st': 'D', 'boot': 'crashed', 'charge': q * 1000,
                                   'pct': 2.4} for i, q in enumerate([90, 80, 75])]   # A sync flood at 2.4%.
        asked = []
        endings = lambda boot: asked.append(boot) or {'died': ('cut', 1.0), 'crashed': ('sync flood', 2.0)}.get(boot)
        found = {d['boot']: d for d in eta.deaths_in(minutes, running='running', endings=endings)}
        self.assertEqual(found['died']['ending'], 'cut')
        self.assertEqual(found['crashed']['ending'], 'sync flood')
        self.assertNotIn('cut', found['crashed'])                       # Remembered as no death.
        self.assertEqual(eta.fit_cutoff(minutes, (), 'running', endings)['deaths'], 1)
        self.assertEqual(eta.fit_cutoff(minutes, (), 'running')['deaths'], 2)   # No journal: as before.
        path = self.root / 'deaths.json'
        known = eta.remember_deaths(minutes, 'running', path, endings)
        self.assertEqual({e['boot'] for e in known}, {'died', 'crashed'})
        asked.clear()
        eta.remember_deaths(minutes, 'running', path, endings)
        self.assertEqual(asked, [])                                      # Known boots aren't asked again.
        self.assertEqual(eta.fit_cutoff(minutes, known, 'running')['deaths'], 1)

    def test_boot_endings_from_the_journal(self):
        boots = [{'boot_id': f'{name}0000aaaa', 'last_entry': 1000_000_000 * (i + 1)} for i, name in
                 enumerate(['cut00000', 'asleep00', 'downdown', 'shortcut', 'flooded0', 'rebooted', 'pressed0',
                            'flushed0', 'now00000'])]
        # The reset reason each boot's successor gives; a finished shutdown can be followed by one
        # (8c3bebb0's by 0x00200800).
        reasons = {'rebooted': '0x08000800', 'pressed0': '0x00080800', 'now00000': '0x00200800'}
        tails = {
            'asleep00': ['PM: suspend entry (s2idle)', 'PM: suspend exit', 'PM: suspend entry (s2idle)'],
            'downdown': ['PM: suspend entry (s2idle)', 'PM: suspend exit', 'System is powering down.',
                         'Stopping Flush Journal to Persistent Storage...'],
            'shortcut': ['System is powering down.'],
            'pressed0': ['Power key pressed short.'],
            'cut00000': ['PM: suspend entry (s2idle)', 'PM: suspend exit'],
            'flushed0': ['System is powering down.', 'Stopping Flush Journal to Persistent Storage...'],
        }

        def run(*args):
            if args[0] == '--list-boots':
                return json.dumps(boots)
            name = args[args.index('-b') + 1][:8]
            index = next(i for i, b in enumerate(boots) if b['boot_id'].startswith(name))
            if args[0] == '-k':
                return (f'x86/amd: Previous system reset reason [{reasons[name]}]: whatever\n'
                        if name in reasons else '')
            end = boots[index]['last_entry']
            return ''.join(json.dumps({'MESSAGE': m, '__REALTIME_TIMESTAMP': str(end - 10_000_000 + k)}) + '\n'
                           for k, m in enumerate(tails.get(name, [])))
        endings = eta.Endings(run)
        self.assertEqual({b: endings(b)[0] for b in ['cut00000', 'asleep00', 'downdown', 'shortcut', 'flooded0',
                                                     'rebooted', 'pressed0', 'flushed0']},
                         {'cut00000': 'cut', 'asleep00': 'asleep', 'downdown': 'shutdown',
                          'shortcut': 'shutdown cut short', 'flooded0': 'sync flood', 'rebooted': 'reboot',
                          'pressed0': 'power key', 'flushed0': 'shutdown'})   # The finished shutdown wins.
        self.assertEqual(endings('cut00000')[1], 1000.0)                 # When its journal ends.
        self.assertIsNone(endings('now00000'))                           # Still running.
        self.assertEqual(endings('unknown0'), ('unknown', None))         # The journal works, but never had it.
        self.assertIsNone(eta.Endings(lambda *args: None)('cut00000'))   # No journalctl: ask again later.

    def test_a_last_gasp_near_the_cutoff_from_another_boot_is_a_death(self):
        gasp, deaths = self.root / 'lastgasp.json', self.root / 'deaths.json'
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
        gasp.write_text('{"boot": "half')                            # Torn: ignored, not a crash.
        self.assertIsNone(eta.harvest_gasp('now', gasp, deaths))

    def test_the_journal_decides_what_a_last_gasp_was(self):
        gasp, deaths = self.root / 'lastgasp.json', self.root / 'deaths.json'

        def harvest(how, **g):
            gasp.write_text(json.dumps({'boot': 'old', 'charge': 0.060, 'full': 3.125, 't': 100.0, 'seconds': 20.0,
                                        'rate': 0.0002, **g}))
            return eta.harvest_gasp('now', gasp, deaths, endings=lambda boot: how)
        self.assertIsNone(harvest(('sync flood', 105.0)))               # A crash at 1.9%: no death.
        self.assertIsNone(harvest(('asleep', 105.0)))
        self.assertIsNone(harvest(('shutdown', 130.0)))                 # Shut down by hand after the warning.
        self.assertIsNone(harvest(('cut', 105.0), charge=0.110))        # 3.5%: no cut is that high (a silent crash).
        cut = harvest(('cut', 105.0), seconds=900)                      # The journal knows better than the guess.
        self.assertAlmostEqual(cut['cut'], 0.0598)
        self.assertEqual((cut['ending'], cut['poweroff']), ('cut', False))
        self.assertTrue(harvest(('shutdown', 130.0), poweroff=True, boot='old2')['poweroff'])   # Its own, finished.
        short = harvest(('shutdown cut short', 130.0), poweroff=True, boot='older')
        self.assertFalse(short['poweroff'])                             # The battery cut the shutdown short:
        self.assertAlmostEqual(short['cut'], 0.060 - 30 * 0.0002)       # a death, by the journal's end.
        self.assertEqual(eta.fit_cutoff([], eta.load_gasps(deaths))['deaths'], 2)

    def test_what_the_journal_cant_say_yet_is_asked_again(self):
        path, gasp = self.root / 'deaths.json', self.root / 'lastgasp.json'
        minutes, answers = self.boots(), {}
        endings = lambda boot: answers.get(boot)                         # None: journalctl failed just now.
        known = eta.remember_deaths(minutes, 'running', path, endings)
        died = next(e for e in eta.load_gasps(path) if e['boot'] == 'died')
        self.assertTrue(died['unconfirmed'])                             # Filed, but not for good:
        self.assertEqual(eta.fit_cutoff(minutes, known, 'running', endings)['deaths'], 1)   # it counts meanwhile,
        answers['died'] = ('sync flood', 1.0)
        known = eta.remember_deaths(minutes, 'running', path, endings)   # and the next refit asks again.
        died = next(e for e in eta.load_gasps(path) if e['boot'] == 'died')
        self.assertEqual((died['ending'], 'cut' in died, 'unconfirmed' in died), ('sync flood', False, False))
        self.assertEqual(eta.fit_cutoff(minutes, known, 'running', endings)['deaths'], 0)
        # A boot the working journal doesn't have (rotated away) counts as without the journal, for good.
        other = self.root / 'other.json'
        eta.remember_deaths(minutes, 'running', other, lambda boot: ('unknown', None))
        died = next(e for e in eta.load_gasps(other) if e['boot'] == 'died')
        self.assertEqual(('cut' in died, 'unconfirmed' in died), (True, False))
        answers.clear()
        # A last gasp: filed unconfirmed with the gasp itself, and judged once the journal answers.
        for boot, poweroff in (('own', True), ('lost', False)):
            gasp.write_text(json.dumps({'boot': boot, 'charge': 0.060, 'full': 3.125, 't': 100.0, 'seconds': 20.0,
                                        'rate': 0.0002, 'poweroff': poweroff}))
            entry = eta.harvest_gasp('now', gasp, path, endings)
            self.assertTrue(entry['unconfirmed'])
        filed = {e['boot']: e for e in eta.load_gasps(path)}
        self.assertTrue(filed['own']['poweroff'])                        # Meanwhile as without the journal:
        self.assertAlmostEqual(filed['lost']['cut'], 0.0598)              # a power-off, and a death.
        answers.update(own=('shutdown cut short', 130.0), lost=('shutdown', 130.0))
        eta.remember_deaths([], 'running', path, endings)
        filed = {e['boot']: e for e in eta.load_gasps(path)}
        self.assertAlmostEqual(filed['own']['cut'], 0.060 - 30 * 0.0002)  # The battery cut its power-off short;
        self.assertFalse(filed['own']['poweroff'] or 'unconfirmed' in filed['own'])
        self.assertEqual((filed['lost']['ending'], 'cut' in filed['lost']), ('shutdown', False))   # shut down by hand.
        asked = []
        eta.remember_deaths([], 'running', path, lambda boot: asked.append(boot))
        self.assertEqual(asked, [])                                      # Settled: not asked again.

    def test_deaths_survive_a_torn_write(self):
        path = self.root / 'deaths.json'
        eta.remember([{'boot': 'a', 't': 1, 'cut': 0.055}], path)
        eta.remember([{'boot': 'b', 't': 2, 'cut': 0.057}], path)
        self.assertEqual(len(json.loads(eta.backup(path).read_text())), 1)   # The version before.
        self.assertFalse(path.with_name('deaths.json.tmp').exists())
        path.write_text('[{"boot": "a", "t"')                             # A crash mid-write (before this fix).
        self.assertEqual([e['boot'] for e in eta.load_gasps(path)], ['a'])
        eta.remember([{'boot': 'c', 't': 3, 'cut': 0.056}], path)
        self.assertEqual({e['boot'] for e in eta.load_gasps(path)}, {'a', 'c'})
        self.assertEqual(len(json.loads(eta.backup(path).read_text())), 1)   # The torn file never became the .bak.

    def test_a_damaged_deaths_json_never_stops_the_warnings_or_the_power_off(self):
        path = self.root / 'deaths.json'
        path.write_text('[{"boot": "a", "t": 1, "cut": 0.055}, 5, null, "x", {"t": 2, "cut": 0.05}, '
                        '{"boot": "b", "t": null, "cut": "0.06"}, {"boot": "c", "t": 3, "cut": 0.057}, '
                        '{"boot": "d", "t": 4, "cut": NaN}, {"boot": 7}]')
        live = make_live(self.root)
        self.assertEqual((live.params['deaths'], round(live.params['cutoff'], 4)), (2, 0.056))   # a and c.
        self.assertTrue(live.warns_off())
        eta.remember([{'boot': 'e', 't': 5, 'cut': 0.056}], path)        # Its odd entries don't stop a write …
        self.assertEqual(eta.fit_cutoff([], eta.load_gasps(path))['deaths'], 3)
        gasp = self.root / 'lastgasp.json'
        gasp.write_text(json.dumps({'boot': 'f', 'charge': 0.056, 'full': 3.125, 't': None, 'seconds': 'soon'}))
        self.assertAlmostEqual(eta.harvest_gasp('now', gasp, path)['cut'], 0.056)   # … nor odd black-box fields,
        gasp.write_text(json.dumps({'boot': None, 'charge': 0.056}))                 # nor one without a boot.
        self.assertIsNone(eta.harvest_gasp('now', gasp, path))
        path.write_text('{"boot": "a"}')                                 # Not a list at all: the .bak.
        self.assertEqual({e['boot'] for e in eta.load_gasps(path)}, {'a', 'b', 'c', 'd', 'e'})   # Before 'f'.
        with mock.patch.object(eta, 'fit_cutoff', side_effect=RuntimeError('odd')):
            live = make_live(self.root)                                  # Whatever goes wrong, Live starts,
        self.assertEqual(live.params['deaths'], 0)
        battery(self.root, **dict(LOW, charge_now=80000))
        live.runtime = self.root / 'lastgasp.json' / 'run'               # and eta.json that can't be written
        live.publish(live.estimate()['detail'])                          # is only reported.
        live.runtime = self.root / 'run'
        live.publish(live.estimate()['detail'])
        self.assertIn('pct', json.loads((self.root / 'run/eta.json').read_text()))

    def test_energy_counts_down_to_the_cutoff(self):
        params = dict(self.params, volts=[14.0] * 101, cutoff=0.056)
        self.assertAlmostEqual(eta.usable_energy(0.556, 3.125, params), 0.5 * 14.0, places=3)
        self.assertEqual(eta.usable_energy(0.04, 3.125, params), 0.0)
        self.assertEqual(eta.usable_energy(0.04, 0.0, params), 0.0)        # No full charge known.
        self.assertEqual(eta.time_to_empty(params, 0.0, 12.0), (0.0, 0.0, 0.0))
        self.assertEqual(eta.empty_distribution(params, 0.0, 12.0, 0.0, 0.0)[2], [1.0])

    def test_at_the_cutoff_it_counts_zero_and_keeps_its_safety_on(self):
        battery(self.root, **dict(LOW, charge_now=50000))               # 50 mAh: below the 56 mAh cutoff.
        offs = []
        clock = Clock()
        live = make_live(self.root, clock, poweroff=lambda: offs.append(1) or True)
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, deaths=5)
        for _ in range(3):
            out = live.estimate()
            clock.tick()
        self.assertIn('critical', out['class'])
        self.assertIn('0s', out['text'])
        self.assertEqual(len(offs), 1)                                   # Powered off …
        self.assertTrue(json.loads((self.root / 'lastgasp.json').read_text())['poweroff'])   # … and said so.

    def test_odd_batteries_never_crash_the_estimate(self):
        clock = Clock()
        live = make_live(self.root, clock)                               # No battery yet.
        self.assertEqual(live.estimate()['text'], '--')
        b = battery(self.root, **dict(LOW, charge_now=500000, charge_full=0, capacity=17))
        out = live.estimate()                                            # It appeared; charge_full reads 0.
        self.assertIn('discharging', out['class'])
        self.assertEqual(out['percentage'], 17)                          # The gauge's own percent.
        (b / 'charge_full').write_text('3125000')
        self.assertIn('\uf252', live.estimate()['text'])

    def test_a_failing_forecast_still_warns_records_and_powers_off(self):
        battery(self.root, **dict(LOW, charge_now=62000))
        notes, offs = [], []
        clock = Clock()
        live = make_live(self.root, clock, notify=lambda *n: notes.append(n), poweroff=lambda: offs.append(1) or True)
        live.params = dict(self.params, volts=[13.8] * 101, tau=0.0, cutoff=0.056, cutoff_spread=0.0018, deaths=5)
        for i in range(60, 0, -1):  # 0.2 mAh/s, to 62 mAh: 30 s to the cutoff.
            live.samples.append((clock.m - 2 * i, 11.3, 'Discharging', 0.062 + 0.0002 * 2 * i))
        out = live.estimate()
        clock.tick()
        live.estimate()
        self.assertTrue(out['text'])                                     # A label all the same,
        self.assertTrue(notes)                                           # the warnings,
        self.assertEqual(len(offs), 1)                                   # the power-off,
        self.assertTrue((self.root / 'lastgasp.json').exists())          # and the black box.

    def test_the_last_minutes_count_down_to_the_second_and_warn(self):
        battery(self.root, **dict(LOW, charge_now=80000, capacity=3))
        notes, clock = [], Clock()
        live = make_live(self.root, clock, notify=lambda *note: notes.append(note))
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018)
        # Two minutes of the gauge falling 0.2 mAh/s, to 80 mAh now: 120 s to the cutoff.
        for i in range(60, 0, -1):
            live.samples.append((clock.m - 2 * i, 11.3, 'Discharging', 0.080 + 0.0002 * 2 * i))
        out = live.estimate()
        detail = out['detail']
        self.assertIn('critical', out['class'])
        self.assertAlmostEqual(detail['cutout'] - clock.t, 120, delta=3)
        self.assertAlmostEqual(detail['cutout_pm'], 18, delta=1)
        self.assertRegex(out['text'], r'</span> [12]m \d\ds <span[^>]*>±18s</span>')
        self.assertEqual(len(notes), 1)                           # Within 5 minutes; once.
        early = time.strftime('%H:%M:%S', time.localtime(clock.t + 120 - 18))
        self.assertEqual(notes[0][0], f'Battery cuts out as soon as {early}')   # The early edge.
        clock.tick()
        live.estimate()
        self.assertEqual(len(notes), 1)
        gasp = json.loads((self.root / 'lastgasp.json').read_text())   # The black box, below 5%.
        self.assertEqual((gasp['boot'], gasp['charge'], gasp['t']), ('cc72fcaf', 0.08, clock.t))
        (self.root / 'class/power_supply/BAT1/status').write_text('Charging')
        live.estimate()
        self.assertFalse((self.root / 'lastgasp.json').exists())       # Plugged in: not a death.

    def test_below_low_battery_the_gauge_times_the_warnings(self):
        # The gauge falls 0.12 mAh/s while the current says half that (it falls faster near empty).
        b = battery(self.root, **dict(LOW, current_now=216000, capacity=4))
        notes, offs, clock = [], [], Clock()
        live = make_live(self.root, clock, notify=lambda *note: notes.append((clock.m, note)),
                         poweroff=lambda: offs.append(clock.m) or True)
        live.params = dict(self.params, level=math.log(3.0), volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0015,
                           deaths=5)                                # The level is the current's 3 W.
        cut = clock.m + 900                                         # From 5.3%, the cut 900 s on.
        with mock.patch('os.fsync'):
            while clock.m < cut and not offs:
                (b / 'charge_now').write_text(str(int((0.056 + 0.00012 * (cut - clock.m)) * 1e6)))
                live.estimate()
                clock.tick()
        first = next(t for t, note in notes if note[0].startswith('Powering off as soon as'))
        # Below 5% the gauge's slope times it: the first notice ~5 minutes before the power-off's early
        # edge and at least 5 before the power-off itself (the watts alone would say twice as long).
        self.assertGreater(cut - first, 300 + eta.POWEROFF_SECONDS)
        self.assertLess(cut - first, 300 + eta.POWEROFF_SECONDS + 200)
        self.assertGreater(offs[0] - first, 300)

    def power_off_lead(self, rate, offset, spread=0.0015):
        """Seconds between the power-off and the cut on a synthetic gauge trace falling
        `rate` Ah/s in 1 mAh steps, the true cut `offset` Ah above the learned cutoff.
        The gauge alone times it here: the forecast is left out (and fast)."""
        root = self.root / f'{rate}-{offset}'
        b = battery(root, **dict(LOW, current_now=int(rate * 3600 * 1e6)))
        offs, clock = [], Clock()
        live = make_live(root, clock, poweroff=lambda: offs.append(clock.m) or True)
        live.params = dict(eta.DEFAULTS, cutoff=0.056, cutoff_spread=spread, deaths=5, volts=[13.8] * 101)
        live.forecast = lambda *args: None
        cut_at = clock.m + 300
        with mock.patch('os.fsync'):                                # The black box's fsyncs: slow, and not the point.
            while clock.m < cut_at and not offs:
                charge = 0.056 + offset + rate * (cut_at - clock.m)
                (b / 'charge_now').write_text(str(int(charge * 1000) * 1000))
                live.estimate()
                clock.tick()
        return cut_at - offs[0] if offs else None

    def test_the_power_off_comes_well_before_the_cut(self):
        # Clean shutdowns here take 3–11 s; the lead was 7–43 s replaying five real deaths before this.
        for rate in (0.0001, 0.0002, 0.0005, 0.001):                 # ~5 to ~50 W.
            for offset in (-0.00225, 0.0, 0.00225):                  # The cut ±1.5 spreads from the learned one.
                lead = self.power_off_lead(rate, offset)
                self.assertIsNotNone(lead, (rate, offset))
                self.assertGreater(lead, 40, (rate, offset))
                self.assertLess(lead, 110, (rate, offset))

    def test_a_draw_that_rises_near_empty_still_gets_its_lead(self):
        # af7f85d1: from 17 to 25 W three minutes from the end, the gauge falling 4x as fast. The 10-minute
        # slope lags it; the 2-minute one takes over as soon as either says 3 minutes (before: a 4 s lead).
        b = battery(self.root, **dict(LOW, current_now=900000))
        notes, offs, clock = [], [], Clock()
        live = make_live(self.root, clock, notify=lambda *n: notes.append((clock.m, n[0])),
                         poweroff=lambda: offs.append(clock.m) or True)
        live.params = dict(eta.DEFAULTS, cutoff=0.056, cutoff_spread=0.0015, deaths=5, volts=[13.8] * 101)
        live.forecast = lambda *args: None
        cut = clock.m + 900
        charge = lambda left: 0.056 + 0.001 * left if left <= 184 else 0.240 + 0.00025 * (left - 184)
        with mock.patch('os.fsync'):
            while clock.m < cut and not offs:
                (b / 'charge_now').write_text(str(int(charge(cut - clock.m) * 1000) * 1000))
                live.estimate()
                clock.tick()
        self.assertGreater(cut - offs[0], 40)
        minute = [t for t, title in notes if title.startswith('Powering off as soon as')][-1]
        self.assertGreater(offs[0] - minute, 30)                         # The 1-minute notice, ahead of it.

    def test_once_a_death_taught_the_cutoff_it_powers_off_cleanly_before_it(self):
        b = battery(self.root, **dict(LOW, charge_now=62000))
        mains(self.root, False)
        notes, offs, results, clock, marked = [], [], [], Clock(), []
        box = lambda: json.loads((self.root / 'lastgasp.json').read_text())['poweroff']
        live = make_live(self.root, clock, notify=lambda *n: notes.append(n),
                         poweroff=lambda: marked.append(box()) or offs.append(clock.m) or results.pop(0))
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018, deaths=5)

        def steps(n, gap=2.0):
            for _ in range(n):
                out = live.estimate()
                clock.tick(gap)
            return out

        def history():
            live.samples.clear()
            for i in range(60, 0, -1):  # 0.2 mAh/s, to 62 mAh: 30 s to the cutoff.
                live.samples.append((clock.m - 2 * i, 11.3, 'Discharging', 0.062 + 0.0002 * 2 * i))
        history()
        out = steps(1)
        self.assertIn('\uf011', out['text'])                      # Counting down to the power-off …
        self.assertAlmostEqual(out['detail']['poweroff'] - out['detail']['updated'], 0, delta=3)
        self.assertEqual(offs, [])                                # … but once is not enough.
        results[:] = [False, True]                                # systemctl poweroff fails the first time:
        steps(1)
        self.assertEqual(len(offs), 1)
        self.assertEqual(notes[-1][0], "Couldn't power off")      # said so,
        self.assertFalse(json.loads((self.root / 'lastgasp.json').read_text())['poweroff'])   # not marked,
        steps(3)
        self.assertEqual(len(offs), 1)                            # waits POWEROFF_RETRY s,
        steps(3)
        self.assertEqual(len(offs), 2)                            # then tries again: done, and marked.
        self.assertTrue(json.loads((self.root / 'lastgasp.json').read_text())['poweroff'])
        self.assertEqual(marked, [True, True])                    # Marked before each call (the shutdown kills us).
        steps(2)
        self.assertEqual(len(offs), 2)                            # Only once.
        live.clear_gasp()
        live.params['deaths'] = 0                                  # Nothing learned: no power-off.
        steps(2)
        self.assertEqual(len(offs), 2)
        live.params['deaths'] = 5
        mains(self.root, True)                                     # Plugged in at the last moment,
        steps(2)                                                   # the status not caught up yet:
        self.assertEqual(len(offs), 2)                             # no power-off.
        (b / 'status').write_text('Discharging')

    def test_the_two_readings_must_come_together(self):
        b = battery(self.root, **dict(LOW, charge_now=62000))
        offs, clock = [], Clock()
        live = make_live(self.root, clock, poweroff=lambda: offs.append(1) or True)
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018, deaths=5)

        def history():
            live.samples.clear()
            for i in range(60, 0, -1):
                live.samples.append((clock.m - 2 * i, 11.3, 'Discharging', 0.062 + 0.0002 * 2 * i))
        history()
        live.estimate()
        clock.tick(8)                                              # A stall: 8 s later is too late.
        live.estimate()
        self.assertEqual(offs, [])
        clock.tick(30)                                             # A suspend between two readings:
        live.estimate()
        self.assertLess(len(live.samples), 5)                      # the trend starts over,
        clock.tick(30)
        live.estimate()
        self.assertEqual(offs, [])                                 # and so does the count.
        clock.tick(8)
        history()
        live.estimate()
        (b / 'status').write_text('Charging')                      # Not discharging in between:
        clock.tick()
        live.estimate()
        (b / 'status').write_text('Discharging')
        clock.tick()
        history()
        live.estimate()
        self.assertEqual(offs, [])                                 # the count started over.
        clock.tick()
        live.estimate()
        self.assertEqual(len(offs), 1)

    def test_timed_by_the_forecast_alone_it_waits_for_the_charge_to_near_the_cutoff(self):
        # No gauge slope yet: the rate is the draw's, 11.3 W at 13.8 V = 0.23 mAh/s, so the 45 s lead
        # (and the interval's half again) needs the charge within ~19 mAh of the cutoff.
        battery(self.root, **dict(LOW, charge_now=140000))
        offs, clock = [], Clock()
        live = make_live(self.root, clock, poweroff=lambda: offs.append(1) or True)
        live.params = dict(self.params, cutoff=0.056, deaths=5)
        timing = {'seconds': 30.0, 'early': 20.0, 'late': 40.0, 'rate': None, 'source': 'forecast'}
        state = {'watts': 11.3, 'volts': 13.8, 'pct': 4.5, 'charge': 0.140, 'full': 3.125, 'mains': False}
        with mock.patch('os.fsync'):
            live.maybe_power_off(state, timing, clock.m, clock.m)                      # 84 mAh above it:
            self.assertEqual(offs, [])                                                 # a wild forecast.
            live.maybe_power_off(dict(state, charge=0.066), timing, clock.m, clock.m)  # 10 mAh above it.
        self.assertEqual(offs, [1])

    def charger_trace(self, plug_at, weak_rate, recover=None):
        """A gauge falling 0.2 mAh/s to the cut at 300 s; mains comes online `plug_at` s in
        while the status stays Discharging, the gauge then falling `weak_rate` (and the
        status turning Charging `recover` s after the plug-in, if it does). Returns the
        power-off time (s before the cut), the labels and the notifications."""
        b = battery(self.root, **dict(LOW, current_now=50000))
        mains(self.root, False)
        offs, notes, labels, clock = [], [], [], Clock()
        live = make_live(self.root, clock, poweroff=lambda: offs.append(clock.m) or True,
                         notify=lambda *n: notes.append((clock.m, n)))
        live.params = dict(eta.DEFAULTS, cutoff=0.056, cutoff_spread=0.0015, deaths=5, volts=[13.8] * 101)
        live.forecast = lambda *args: None
        start = clock.m
        charge = lambda m: 0.056 + 0.0002 * (300 - min(m, plug_at)) - weak_rate * max(0.0, m - plug_at)
        with mock.patch('os.fsync'):
            while clock.m - start < 600 and not offs:
                m = clock.m - start
                if m >= plug_at:
                    mains(self.root, True)
                if recover is not None and m >= plug_at + recover:
                    (b / 'status').write_text('Charging')
                (b / 'charge_now').write_text(str(int(charge(m) * 1e6 // 1000 * 1000)))
                labels.append((m, live.estimate()))
                clock.tick()
        return (300 + start - offs[0] if offs else None), labels, notes

    def test_only_a_plug_in_that_stops_the_discharge_stops_the_power_off(self):
        # A plug-in as the countdown nears zero, the status catching up 8 s later: no power-off, no alarm.
        off, labels, notes = self.charger_trace(plug_at=230, weak_rate=0.0002, recover=8)
        self.assertIsNone(off)
        self.assertIn('charging', labels[-1][1]['class'])
        self.assertFalse([n for t, n in notes if t - 5000 >= 230])
        # Mains merely online, the battery still falling as fast: the charger can't keep up. It holds
        # the power-off WEAK_SECONDS, then times it by the gauge since mains came online.
        off, labels, notes = self.charger_trace(plug_at=120, weak_rate=0.00015)
        cut = 300 + (300 - 120) * (0.0002 / 0.00015 - 1)                # Later: the charger slows the fall.
        self.assertGreater(off + cut - 300, 40)                          # Still well before the cut.
        weak = [m for m, out in labels if 'weak-charger' in out['class']]
        self.assertAlmostEqual(weak[0], 120 + eta.WEAK_SECONDS, delta=2)
        self.assertIn('charger too weak', labels[-1][1]['text'])
        self.assertEqual(labels[-1][1]['detail']['charger'], 'too weak')
        self.assertIn("The charger can't keep up", notes[-1][1][1])
        # A weak charger plugged in 20 s before the power-off was due: never a power-off in its first
        # WEAK_SECONDS (as before this, when mains online always held it), only after, if still in time.
        off, labels, notes = self.charger_trace(plug_at=220, weak_rate=0.0002)
        self.assertTrue(off is None or 300 - off >= 220 + eta.WEAK_SECONDS, off)
        # A charger that keeps up (the gauge stops falling) never powers off, Discharging or not.
        off, labels, notes = self.charger_trace(plug_at=200, weak_rate=0.0)
        self.assertIsNone(off)

    def test_trends_follow_the_boot_clock_not_the_wall(self):
        battery(self.root, **dict(LOW, charge_now=80000, capacity=3))
        clock = Clock()
        live = make_live(self.root, clock)
        live.params = dict(self.params, volts=[13.8] * 101, cutoff=0.056, cutoff_spread=0.0018)
        for i in range(60, 0, -1):
            live.samples.append((clock.m - 2 * i, 11.3, 'Discharging', 0.080 + 0.0002 * 2 * i))
        clock.t += 3600                                            # NTP moves the wall clock an hour.
        self.assertAlmostEqual(live.estimate()['detail']['cutout'] - clock.t, 120, delta=3)

    def test_a_restart_seeds_the_recent_draw_from_the_log(self):
        clock = Clock()
        live = make_live(self.root, clock)
        minutes = [{'t': clock.t - 60 * k - 60, 'st': 'D', 'bat': 12.0, 'charge': 900000 + 1000 * k, 'unit': 'uAh'}
                   for k in range(14, -1, -1)] + [{'t': clock.t - 3000, 'st': 'D', 'bat': 40.0}]   # The last too old.
        live.seed(minutes)
        self.assertEqual(len(live.samples), 15)
        self.assertEqual(live.recent('Discharging', 600, clock.m), 12.0)
        self.assertEqual(live.samples[-1][0], clock.m)
        self.assertEqual(live.samples[-1][3], 0.9)

    def test_the_cutoff_comes_from_deaths_json_without_models(self):
        eta.remember([{'boot': f'd{i}', 't': i, 'cut': 0.055 + 0.001 * i} for i in range(3)], self.root / 'deaths.json')
        live = make_live(self.root)
        self.assertEqual((live.params['deaths'], round(live.params['cutoff'], 4)), (3, 0.056))
        self.assertTrue(live.warns_off())                          # The power-off stays armed.

    def test_refits_wait_for_the_database_and_never_block_the_estimate(self):
        db = self.root / 'power.sqlite3'
        conn = sqlite3.connect(db)
        conn.execute(f'PRAGMA user_version = {store.VERSION - 1}')  # Mid-migration.
        conn.close()
        ready = store.minutes_ready
        self.addCleanup(lambda: setattr(store, 'minutes_ready', ready))
        self.assertFalse(eta.db_ready(db))
        self.assertFalse(eta.db_ready(self.root / 'absent.sqlite3'))
        clock = Clock()
        live = make_live(self.root, clock)
        before = live.params
        self.assertFalse(live.refit())
        self.assertIs(live.params, before)                         # Nothing trained on it,
        self.assertEqual(live.next_fit, clock.m + eta.RETRY)       # and again in a minute.
        asked = []
        store.minutes_ready = lambda path: asked.append(path) and False   # Minutes still to recompute.
        self.assertFalse(eta.db_ready(db))
        self.assertEqual(asked, [db])
        store.minutes_ready = lambda path: True
        self.assertTrue(eta.db_ready(db))
        store.minutes_ready = lambda path: 1 / 0
        self.assertFalse(eta.db_ready(db))
        # In the background: the estimate goes on while a slow refit runs, and none starts when low.
        release, started = threading.Event(), []
        battery(self.root, **dict(LOW, charge_now=100000))           # 3%, at start:
        live = make_live(self.root, clock)
        live.refit = lambda: started.append(1) or release.wait(5)
        live.maybe_refit()
        self.assertEqual(started, [])                              # not before an estimate has read the battery,
        live.estimate()
        live.maybe_refit()
        self.assertEqual(started, [])                              # nor when that says it is low.
        (self.root / 'class/power_supply/BAT1/charge_now').write_text('1000000')   # 32%
        live.estimate()
        live.maybe_refit()
        self.assertEqual(live.estimate()['class'][1], 'discharging')   # The estimate goes on meanwhile.
        live.maybe_refit()
        self.assertEqual(started, [1])                             # One at a time.
        release.set()
        live.fitting.join(5)
        (self.root / 'class/power_supply/BAT1/charge_now').write_text('100000')   # 3%
        live.estimate()
        live.next_fit = 0
        live.maybe_refit()
        self.assertEqual(started, [1])

    def test_countdown_format(self):
        self.assertEqual((eta.countdown(247.4), eta.countdown(41), eta.countdown(-2)), ('4m 07s', '41s', '0s'))

    def test_plus_minus_is_half_the_interval(self):
        self.assertIn('±0:15', eta.plus_minus(1.0, 1.5))

    def test_clock_format(self):
        self.assertEqual(eta.clock(1.5), '1:30')
        self.assertEqual((eta.clock(2.0), eta.clock(10.25)), ('2:00', '10:15'))
        self.assertEqual(eta.clock(0.01), '0:01')
        self.assertEqual(eta.clock(float('inf')), '--')


if __name__ == '__main__':
    unittest.main()
