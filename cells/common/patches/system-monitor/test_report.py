import collections
import itertools
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import scipy.stats

import power
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
            load, bl, ghz = rng.uniform(1, 30), rng.uniform(.1, 1), rng.uniform(.8, 3)
            soc = 3 + 2 * ghz + .2 * load + rng.normal(0, .1)
            rows.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'ghz': ghz, 'soc': soc, 'bl': bl,
                         'bat': .5 + 1.4 * soc + 3 * bl + rng.normal(0, .1)})
        model = report.fit_rows(report.battery_rows(rows))
        self.assertAlmostEqual(model['floor'], 3, delta=.3)
        self.assertAlmostEqual(model['clock'], 2, delta=.2)
        self.assertAlmostEqual(model['load'], .2, delta=.01)
        self.assertAlmostEqual(model['idle_ghz'], np.percentile([r['ghz'] for r in rows], 5))
        self.assertAlmostEqual(model['chip'], 1.4, delta=.05)
        self.assertAlmostEqual(model['bl'], 3, delta=.2)
        self.assertAlmostEqual(model['usb'], report.PRIOR['usb'], delta=.01)  # Never varied → prior

    def test_attribution_sums_to_battery_power(self):
        model = dict(report.PRIOR, floor=5, load=.2, clock=1, idle_ghz=1, chip=1.4, bl=3)
        m = {'t': 0, 'dt': 60, 'st': 'D', 'bat': 20.0, 'soc': 8.0, 'load': 10.0, 'ghz': 2.0, 'bl': .5,
             'apps': {'Floorp': [6.0, 0, 0], 'openroad': [2.0, 0, 0]}}
        parts = report.attribute(m, model)
        self.assertAlmostEqual(sum(parts['groups'].values()), 20.0)
        self.assertAlmostEqual(parts['groups']['Processor baseline'], 8.4)  # 1.4 × (5 W + 1 W/GHz × 1 GHz idle)
        self.assertAlmostEqual(parts['apps']['Floorp'] / parts['apps']['openroad'], 3)  # The clock above idle too
        self.assertAlmostEqual(parts['groups']['Display'], 1.5)
        parts = report.attribute(dict(m, ghz=.8), model)  # Below the idle clock: all of it is baseline.
        self.assertAlmostEqual(parts['groups']['Processor baseline'], 1.4 * 5.8)

    def test_runtime_gain(self):
        self.assertAlmostEqual(report.runtime_gain(48, 16, 2), 60 * 48 * (1 / 14 - 1 / 16))
        self.assertEqual(report.runtime_gain(48, 16, 0), 0)
        self.assertLess(report.runtime_gain(48, 16, -1), 0)

    def test_what_if_ranks_and_brackets(self):
        rng = np.random.default_rng(2)
        rows = []
        for i in range(300):
            heavy = rng.uniform(0, 20)
            load, ghz = 2 + heavy, 1 + heavy / 20
            soc = 3 + 2 * ghz + .2 * load
            bl = rng.uniform(.3, 1)
            rows.append({'t': i * 60, 'dt': 60, 'st': 'D', 'load': load, 'ghz': ghz, 'soc': soc, 'bl': bl,
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
        for boot in range(10):
            psr = boot % 2 == 1
            for _ in range(60):
                load = rng.uniform(1, 20)
                minutes.append({'t': t, 'dt': 60, 'st': 'D', 'load': load, 'ghz': 1.5, 'soc': 5 + .2 * load, 'bl': .5,
                                'bat': 9 + .3 * load - (1.5 if psr else 0) + rng.normal(0, .5),
                                'boot': f'b{boot}', 'set': {'psr': psr}})
                t += 60
        effect = report.boot_effect(minutes, 48, 16, replicates=200)
        self.assertAlmostEqual(effect['watts'], 1.5, delta=.3)
        self.assertLess(effect['watts_low'], effect['watts'])
        self.assertGreater(effect['minutes'], 0)
        progress = report.boot_effect(minutes[:480], 48, 16)   # Four boots per arm: not enough yet
        self.assertNotIn('watts', progress)
        self.assertEqual(progress['boots'], {'off': 4, 'on': 4})

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
        noisy = report.sleep_runtime([dict(s, watts=s['watts'] ** 3) for s in sleeps], 24.0)
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
        states = report.lever_states(logged, {'hz': 60.0}, chosen={'apst': '100000', 'refresh': '2256x1504@59.999'})
        self.assertEqual((states['apst'], states['abm'], states['refresh'], states['profile'], states['wifi-ps']),
                         (True, True, False, True, False))
        self.assertTrue(report.saving('profile', 'power-saver') and report.saving('abm', '3'))

    def test_boost_comes_from_the_frequency_cap_not_the_global_switch(self):
        top = {'hz': 60.0, 'mhz': 4972}
        # power-saver turned boost off per CPU while the global switch (and the experiment) said on.
        self.assertTrue(report.lever_states({'boost': '1', 'mhz': 3501}, top, chosen={'boost': '1'})['boost'])
        self.assertFalse(report.lever_states({'boost': '1', 'mhz': 4972}, top)['boost'])
        self.assertTrue(report.lever_states({'boost': '0'}, top)['boost'])  # Minutes from before the cap was kept.
        # Since layout 6 the CPU's own maximum says if boost is on, under a cap too.
        capped = report.lever_states({'mhz': 2000, 'cpu_mhz': 4972, 'gpu_dpm': 'low'}, dict(top, cpu_mhz=4972))
        self.assertEqual((capped['boost'], capped['cpu-cap'], capped['gpu-dpm']), (False, True, True))
        plain = report.lever_states({'mhz': 3501, 'cpu_mhz': 3501, 'gpu_dpm': 'auto'}, top)
        self.assertEqual((plain['boost'], plain['cpu-cap'], plain['gpu-dpm']), (True, False, False))
        self.assertFalse(report.lever_states({'mhz': 3501}, top, chosen={'cpu-cap': 'none'})['cpu-cap'])
        self.assertTrue(report.lever_states({'throttle': 'on'}, top)['throttle-apps'])

    def test_your_settings_are_the_latest_outside_experiments(self):
        minutes = factorial_minutes(blocks=4)
        own = dict(minutes[0], t=minutes[0]['t'] - 60, exp=None,
                   set={'aspm': 'performance', 'apst': '0', 'boost': '1', 'hz': 60.0})
        states = report.own_settings([own] + minutes)
        self.assertEqual((states['aspm'], states['apst'], states['boost']), (False, False, False))
        # On AC: the latest minute, but the profile, ABM and boost as they were on battery.
        plugged = dict(own, t=minutes[-1]['t'] + 60, st='C',
                       set={'aspm': 'powersupersave', 'apst': '0', 'boost': '0', 'profile': 'balanced', 'hz': 60.0})
        states = report.own_settings([own] + minutes + [plugged])
        self.assertEqual((states['aspm'], states['boost'], states['profile']), (True, False, False))

    def test_fast_paths_match_their_straightforward_versions(self):
        rng = np.random.default_rng(2)
        x, y, w = rng.normal(size=(200, 12)), rng.normal(size=200), rng.uniform(0.5, 2, 200)
        penalty = np.r_[np.full(4, 1e-6), np.full(8, 0.3)]
        sw = np.sqrt(w)
        stacked = np.linalg.lstsq(np.vstack([x * sw[:, None], np.diag(np.sqrt(penalty))]),
                                  np.concatenate([y * sw, np.zeros(12)]), rcond=None)[0]
        np.testing.assert_allclose(report.ridge(x, y, w, penalty), stacked, atol=1e-10)
        for n in (1, 2, 3, 10, 151):  # Moving-block resamples, as the concatenated ranges they replaced.
            a, b = np.random.default_rng(n), np.random.default_rng(n)
            starts = a.integers(0, max(1, n - 3 + 1), size=math.ceil(n / 3))
            expected = np.concatenate([np.arange(s, min(s + 3, n)) for s in starts])[:n]
            np.testing.assert_array_equal(report.block_indices(n, 3, b), expected)

    def test_no_experiments_means_no_effects(self):
        minutes = [dict(m, exp=None) for m in factorial_minutes(blocks=20)]
        self.assertEqual(report.lever_effects(minutes, 50, 15)['effects'], [])



def block_minutes(t, config, block, run='auto-1', qol=0, present=1.0, bat=15.0):
    """One 4-minute experiment block of minutes (the first is washout) with `config` {name: saving}."""
    settings = {'aspm': 'powersupersave' if config.get('aspm') else 'performance',
                'apst': '100000' if config.get('apst') else '0', 'wifi_ps': 'on' if config.get('wifi-ps') else 'off',
                'boost': '0' if config.get('boost') else '1', 'hz': 48.0 if config.get('refresh') else 60.0}
    return [{'t': t + 60 * i, 'dt': 60, 'st': 'D', 'bat': bat, 'soc': 8, 'load': 4, 'busy': 2, 'gpu': 0, 'video': 0,
             'bl': .5, 'kbd': 0, 'wifi': 0, 'disk': 0, 'usb': 0, 'audio': 0, 'set': settings, 'qol': qol,
             'present': present, 'exp': [run, '+'.join(config), block, '', i == 0, None]} for i in range(4)]


class PlanTests(unittest.TestCase):
    def test_quality_of_life_model_starts_from_priors_and_learns_drops(self):
        model = report.qol_model([], [])
        self.assertAlmostEqual(report.qol_risk(model, {'boost': True}), 0.048, delta=0.005)
        self.assertAlmostEqual(report.qol_risk(model, {'aspm': True}), 0.02, delta=0.003)
        self.assertGreater(report.qol_risk(model, {'boost': True, 'refresh': True}), report.QOL_RISK)
        minutes, events, t = [], [], 600_000
        for block in range(40):
            config = {'boost': block % 2 == 0, 'aspm': block % 4 < 2}
            minutes += block_minutes(t, config, block)
            if config['boost'] and block % 8 in (0, 2):  # Boost off earns a 👎 half the time, ASPM or not.
                events += [{'event': 'qol', 't1': t + 150, 'vote': 'down'}, {'event': 'qol', 't1': t + 230, 'vote': 'up'}]
            t += 240
        model = report.qol_model(minutes, events)
        self.assertGreater(report.qol_risk(model, {'boost': True}), 0.3)
        self.assertLess(report.qol_risk(model, {'aspm': True}), 0.05)
        self.assertEqual(model['down']['tally']['boost']['on'], {'votes': 10, 'windows': 20})
        self.assertGreater(report.qol_risk(model, {'boost': True}), report.QOL_RISK)
        self.assertLess(report.qol_risk(model, {'aspm': True, 'boost': False}), report.QOL_RISK)

    def test_thumbs_up_finds_what_is_especially_nice_and_the_goal_picks_between_that_and_battery(self):
        minutes, events, t, rng = [], [], 600_000, np.random.default_rng(5)
        for block in range(120):
            config = {'refresh': block % 2 == 0, 'aspm': block % 4 < 2}
            # Lower refresh saves 1 W and ASPM 2 W, but full refresh often feels especially smooth.
            bat = 16 - 1.0 * config['refresh'] - 2.0 * config['aspm'] + rng.normal(0, 0.5)
            minutes += block_minutes(t, config, block, run='r', bat=bat)
            if not config['refresh'] and block % 6 == 1:
                events.append({'event': 'qol', 't1': t + 150, 'vote': 'up'})
            t += 240
        models = report.qol_model(minutes, events)
        self.assertGreater(report.delight(models, {'refresh': False}), 3 * report.delight(models, {'refresh': True}))
        levers = report.lever_effects(minutes, energy=50, power=15, replicates=20)
        levers['current'] = dict.fromkeys(levers['current'], False)
        allowed = {'refresh', 'aspm'}
        battery = report.best_config(levers, allowed)
        comfort = report.best_config(levers, allowed, key=lambda c: (-report.delight(models, c),
                                                                    report.predicted(levers['coef'], c)))
        self.assertEqual(battery['config'], {'refresh': True, 'aspm': True})
        self.assertEqual(comfort['config'], {'refresh': False, 'aspm': True})  # ASPM is free: keep its saving.
        recs = report.recommendations(levers, models, energy=50, power=15)
        self.assertEqual(sorted(recs['battery']['apply']), ['aspm=saving', 'refresh=saving'])
        self.assertEqual(sorted(recs['comfort']['apply']), ['aspm=saving', 'refresh=normal'])  # All of it.
        self.assertEqual(recs['comfort']['change'], 'ASPM\u00a0on')
        self.assertRegex(recs['comfort']['delight'], r'^\d+% → \d+%$')
        self.assertRegex(recs['battery']['gain'], r'^\+\d+ min$')
        self.assertEqual(recs['comfort']['text'], f"ASPM\u00a0on · 👍 {recs['comfort']['delight']} · "
                                                  f"{recs['comfort']['gain']} ({recs['comfort']['interval']})")
        # Without a 👍 to learn from, Comfort is Battery's combination.
        unvoted = report.recommendations(levers, report.qol_model(minutes, []), energy=50, power=15)
        self.assertEqual(unvoted['comfort']['apply'], unvoted['battery']['apply'])
        self.assertEqual(unvoted['comfort']['change'], 'same as Battery until a 👍')

    def test_gain_parts(self):
        self.assertEqual(report.gain_parts({'minutes': 14.2, 'low': -8.4, 'high': 51}), ('+14 min', '−8 … +51'))
        self.assertEqual(report.gain_text({'minutes': -6, 'low': None, 'high': None}), '−6 min')
        self.assertEqual(report.gain_parts(None), ('', ''))

    def test_present_users_only_get_combinations_they_are_unlikely_to_mind(self):
        names = ['aspm', 'apst', 'boost', 'profile']
        here = report.plan([], [], names, present=True, rng=np.random.default_rng(1))
        away = report.plan([], [], names, present=False, rng=np.random.default_rng(1))
        self.assertEqual(away['allowed'], 16)
        self.assertLess(here['allowed'], 16)  # At most one noticeable setting at a time, to begin with.
        self.assertFalse(here['choice']['boost'] and here['choice']['profile'])

    def test_blocks_stay_with_combinations_that_might_be_best(self):
        names, minutes, t, rng = ['aspm', 'apst', 'wifi-ps'], [], 600_000, np.random.default_rng(4)
        for block in range(40):  # ASPM clearly saves 3 W; the others nothing.
            config = {n: bool(rng.integers(2)) for n in names}
            minutes += block_minutes(t, config, block, bat=16 - 3 * config['aspm'] + rng.normal(0, 0.5))
            t += 240
        plans = [report.plan(minutes, [], names, present=False, rng=np.random.default_rng(seed),
                             group=report.block_group('auto-1', t)) for seed in range(60)]
        costly = sum(not p['choice']['aspm'] for p in plans) / len(plans)
        apst = sum(p['choice']['apst'] for p in plans) / len(plans)
        self.assertLess(costly, 0.2)  # Blocks without ASPM cost ~3 W, so few are spent there…
        self.assertTrue(0.2 < apst < 0.8)  # …while the uncertain settings still go both ways.
        cost = lambda on: np.mean([p['cost'] for p in plans if p['choice']['aspm'] == on] or [np.nan])
        self.assertLess(cost(True), cost(False))  # The expected cost, in W above the best.

    def test_more_testing_is_worth_it_while_the_recommendation_is_unsure(self):
        names = ['aspm', 'apst', 'wifi-ps']

        def value(blocks, saves, noise, gap):
            rng, minutes, t = np.random.default_rng(4), [], 600_000
            for block in range(blocks):
                config = {n: bool(rng.integers(2)) for n in names}
                minutes += block_minutes(t, config, block, bat=16 - saves * config['aspm'] + rng.normal(0, noise))
                t += 240 + gap
            return report.plan(minutes, [], names, present=False, rng=np.random.default_rng(0),
                               group=report.block_group('auto-1', t))['value']
        unsure = value(16, 1.0, 1.5, 0)                 # An hour of noisy blocks: ASPM's 1 W isn't sure yet.
        sure = value(400, 3.0, 0.3, 29 * 86400 // 400)  # A month of clear ones, under an hour on battery a day.
        self.assertTrue(unsure['worth'])
        self.assertFalse(unsure['choice']['aspm'])      # The recommendation changes nothing yet,
        self.assertGreater(unsure['loss'], 0.5)
        self.assertGreater(unsure['rows'][0]['saves'], 3 * unsure['rows'][0]['costs'])  # and 10 h would help.
        self.assertFalse(sure['worth'])
        self.assertTrue(sure['choice']['aspm'])
        self.assertLess(sure['loss'], 0.1)
        self.assertAlmostEqual(sure['horizon_hours'], 400 * 4 / 60, delta=0.5)  # Its battery hours a day × 30.
        for each in (unsure, sure):
            for row in each['rows']:
                self.assertAlmostEqual(row['saves'], (each['horizon_hours'] - row['hours']) * (each['loss'] - row['loss']))
                self.assertAlmostEqual(row['costs'], row['hours'] * each['cost'])
        self.assertTrue(report.plan([], [], names, present=False)['value']['worth'])  # Nothing logged: test.

    def test_the_value_of_testing_matches_the_exact_answer_for_one_setting(self):
        # ASPM saves 0.5 ± 0.5 W: not sure enough to change (it needs 90%). Blocks measure it with 10 W
        # of noise each, so after h hours (15 blocks each) the posterior sd is s1 and its mean has moved
        # by N(0, 0.25 - s1²); it changes iff that mean is 1.28 s1 below zero.
        value = report.testing_value([(np.array([True]), 0, 10.0, np.array([[4.0]]), np.array([[0.0], [1.0]]))],
                                     np.array([-0.5]), np.array([[0.25]]), ['aspm'], [{'aspm': False}, {'aspm': True}],
                                     np.array([0.0, 1.0]), {'aspm': False}, 10.0, draws=200_000)
        norm = scipy.stats.norm
        above = lambda m, s: m * norm.cdf(m / s) + s * norm.pdf(m / s)  # E[max(0, N(m, s²))]

        def exact(hours):
            s1 = math.sqrt(1 / (4.0 + hours * 15 / 100))
            spread = math.sqrt(0.25 - s1 ** 2)
            m = np.linspace(-0.5 - 8 * spread, -0.5 + 8 * spread, 20001)
            w = norm.pdf(m, -0.5, spread)
            return float(w @ np.where(m <= s1 * norm.ppf(0.1), above(m, s1), above(-m, s1)) / w.sum())
        self.assertFalse(value['choice']['aspm'])
        self.assertAlmostEqual(value['loss'], above(0.5, 0.5), delta=0.005)  # E[max(0, -θ)] = 0.542 W.
        for row in value['rows']:
            self.assertAlmostEqual(row['loss'], exact(row['hours']), delta=0.005)
        self.assertEqual([r['hours'] for r in value['rows']], [10, 30, 100])

    def test_the_cautious_choice_changes_only_what_surely_saves(self):
        names = ['aspm', 'apst']
        options = [dict(zip(names, c)) for c in itertools.product((False, True), repeat=2)]
        x = np.array([report.features(c, names) for c in options])
        singles = np.array([report.features(c, names) for c in ({'aspm': True, 'apst': False},
                                                                  {'aspm': False, 'apst': True})])
        flips = np.array([[float(c[n]) for n in names] for c in options])
        mean = np.array([[-2.0], [-0.5], [0.0]])  # ASPM saves 2 W for sure, APST 0.5 W perhaps; no interaction.
        choose = lambda sd: options[report.cautious_choice(mean, np.diag(sd) ** 2, x, 0, singles, flips)[0]]
        self.assertEqual(choose([0.1, 1.0, 0.01]), {'aspm': True, 'apst': False})  # Not the likeliest best,
        self.assertEqual(choose([0.1, 0.1, 0.01]), {'aspm': True, 'apst': True})   # until APST is sure too.

    def test_the_design_covers_every_combination(self):
        names, minutes, t, rng = ['aspm', 'apst', 'wifi-ps'], [], 600_000, np.random.default_rng(3)
        seen, states = collections.Counter(), collections.Counter()
        for block in range(24):
            choice = report.plan(minutes, [], names, present=False, rng=rng,
                                 group=report.block_group('auto-1', t))['choice']
            seen[tuple(choice[n] for n in names)] += 1
            states.update((n, choice[n]) for n in names)
            minutes += block_minutes(t, choice, block, bat=15 + rng.normal(0, 1))
            t += 240
        # Nothing saves anything, so every setting keeps going both ways.
        self.assertGreaterEqual(min(states[n, on] for n in names for on in (False, True)), 6)
        self.assertGreaterEqual(len(seen), 6)
        self.assertLessEqual(max(seen.values()), 8)


T0 = 1_790_000_040.0  # On a minute: 2026-09-21.


def minute(t, **values):
    """One battery minute with everything the models read."""
    m = {'t': t, 'dt': 60, 'st': 'D', 'bat': 14.0, 'soc': 8.0, 'load': 4.0, 'busy': 1.0, 'ghz': 1.3, 'gpu': 0,
         'video': 0, 'bl': .5, 'kbd': 0, 'wifi': 0, 'disk': 0, 'usb': 0, 'audio': 0, 'set': {'profile': 'low-power'}}
    m.update(values)
    return m


def chip_minutes(n, seed, balanced=0.15, active=False):
    """Minutes of a 16-CPU chip: `a` active cores at clock f, the idle ones at
    their profile's placeholder (0.41 GHz power-saver, 1.12 balanced), so the
    mean clock (ghz) depends on the profile while the power does not; ghz is
    correlated with load about as on the real machine (0.7). With `active`,
    the power follows the active cores' clock (logged as ghz_active) instead."""
    rng, out = np.random.default_rng(seed), []
    for i in range(n):
        profile = 'balanced' if rng.random() < balanced else 'low-power'
        a = 1 + rng.poisson(2.0)
        f = min(4.8, 1.4 + 0.25 * a + rng.gamma(2, 0.3))
        busy = a * rng.uniform(0.3, 0.9)
        p = 1.12 if profile == 'balanced' else 0.41
        ghz = (a * f + (16 - a) * p) / 16
        load = busy * ghz ** 2
        if active:
            soc = 2.0 + 0.9 * f + 0.15 * load + rng.normal(0, .15)
        else:
            soc = 2.0 + 3.0 * (ghz - p) + 0.15 * load + rng.normal(0, .15)
        bl = rng.uniform(.2, 1)
        m = minute(T0 + 60 * i, soc=soc, ghz=ghz, load=load, busy=busy, bl=bl,
                   bat=.5 + 1.4 * soc + 3 * bl + rng.normal(0, .2), set={'profile': profile},
                   apps={'app': [load * 0.7, 0, 0]})
        if active:
            m['ghz_active'] = f
        out.append(m)
    return sorted(out, key=lambda m: m['t'])


def chip_error(model, rows, profile=None):
    """Mean chip prediction error (W) of `model` on `rows` of `profile`."""
    rows = [m for m in rows if profile is None or m['set']['profile'] == profile]
    x, y, _, _ = report.design(rows, model)
    levers = report.model_levers(model)
    coef = np.array([model[n] for n in report.CHIP_TERMS] + [model[f'soc:{n}'] for n in levers])
    return x @ coef - y


class ModelDataTests(unittest.TestCase):
    def test_models_learn_from_experiment_minutes_with_each_setting_held_at_yours(self):
        # Since 2026-10-12 every battery minute has been an experiment's: the models
        # must use them (not freeze on older days), with ASPM's saving kept out of the parts.
        rng, minutes = np.random.default_rng(3), []
        for block in range(150):
            aspm = bool(rng.integers(2))
            for i in range(4):
                load, bl = rng.uniform(1, 20), rng.uniform(.2, 1)
                ghz = 0.9 + 0.08 * load + rng.normal(0, .15)
                soc = 2 + 3 * ghz + .1 * load + rng.normal(0, .1)
                minutes.append(minute(T0 + 60 * len(minutes), soc=soc, ghz=ghz, load=load, bl=bl,
                                      bat=(30.0 if i == 0 else .5 + 1.4 * soc + 3 * bl - 1.5 * aspm + rng.normal(0, .1)),
                                      set={'profile': 'low-power', 'aspm': 'powersupersave' if aspm else 'performance'},
                                      exp=['auto-1', 'aspm', block, 'B' if aspm else 'A', i == 0,
                                           'powersupersave' if aspm else 'performance']))
        plugged = [minute(T0 + 60 * (len(minutes) + i), st='C', bat=-20.0,
                          set={'profile': 'balanced', 'aspm': 'performance'}) for i in range(30)]
        rows = report.battery_rows(minutes + plugged)
        self.assertEqual(len(rows), 450)                       # Washout out, experiments in
        self.assertEqual(report.battery_rows(minutes, include_experiments=False), [])
        model = report.fit_rows(rows, minutes=minutes + plugged)
        self.assertEqual((model['rows'], model['experiment_rows'], model['prior'], model['kept']), (450, 450, 0, 0))
        self.assertEqual((model['t0'], model['t1']), (rows[0]['t'], rows[-1]['t']))
        self.assertAlmostEqual(model['bat:aspm'], -1.5, delta=.2)  # The setting's own column…
        self.assertAlmostEqual(model['chip'], 1.4, delta=.05)      # …so the parts stay right.
        self.assertAlmostEqual(model['bl'], 3, delta=.2)
        self.assertAlmostEqual(model['clock'], 3, delta=.3)
        self.assertEqual(model['own:aspm'], 0.0)                # Yours: ASPM off, from outside experiments
        aspm_on = np.mean([report.minute_states(m, report.model_top(model))['aspm'] for m in rows])
        typical = report.typical_power(rows, model)
        self.assertAlmostEqual(typical - np.mean([m['bat'] for m in rows]), 1.5 * aspm_on, delta=.15)
        _, info = report.what_if(rows, 50, replicates=10, model=model)
        self.assertAlmostEqual(info['power'], typical)
        self.assertEqual((info['rows'], info['experiment_rows']), (450, 450))

    def test_too_few_rows_keep_the_last_model_and_never_silently_the_prior(self):
        rows = report.battery_rows(chip_minutes(200, 1))
        model = report.fit_rows(rows)
        kept = report.fit_rows(rows[:5], previous=model)
        self.assertEqual((kept['kept'], kept['floor'], kept['rows'], kept['t1']),
                         (1.0, model['floor'], model['rows'], model['t1']))
        self.assertEqual(report.fit_rows([], previous=kept)['floor'], model['floor'])  # Kept again
        prior = report.fit_rows(rows[:5])
        self.assertEqual((prior['prior'], prior['rows'], prior['floor']), (1.0, 5.0, report.PRIOR['floor']))
        self.assertEqual(report.fit_rows([], previous=prior)['prior'], 1.0)  # A prior is not a model to keep.
        old = {k: v for k, v in model.items() if k != 'clock_active'}  # From before the active clock
        self.assertEqual(report.fit_rows([], previous=old)['clock_active'], report.PRIOR['clock_active'])

    def test_minutes_partly_on_ac_are_left_out(self):
        m = minute(T0)
        rows = report.battery_rows([m, dict(m, purity=0.95), dict(m, purity=0.6), dict(m, purity=None)])
        self.assertEqual([r.get('purity') for r in rows], [None, 0.95, None])

    def test_your_settings_without_any_battery_minute_outside_experiments(self):
        exp = factorial_minutes(blocks=8)
        plugged = [dict(exp[-1], t=exp[-1]['t'] + 60 * (i + 1), st='C', exp=None,
                        set={'aspm': 'performance', 'apst': '0', 'boost': '1', 'profile': 'balanced', 'abm': 0,
                             'hz': 60.0}) for i in range(5)]
        states = report.own_settings(exp + plugged)
        # AC has AC's profile, ABM and boost: on battery udev sets power-saver, ABM on, no boost.
        self.assertEqual((states['profile'], states['abm'], states['boost'], states['aspm']), (True, True, True, False))


class ClockTests(unittest.TestCase):
    def test_fit_recovers_clock_and_load_correlated_as_on_the_machine(self):
        rng, rows = np.random.default_rng(4), []
        for i in range(400):
            load = rng.gamma(2, 3)
            ghz = 0.9 + 0.08 * load + rng.normal(0, .25)  # corr(ghz, load) ≈ 0.7, as measured
            soc = 3 + 2 * ghz + .2 * load + rng.normal(0, .1)
            rows.append(minute(i * 60, load=load, ghz=ghz, soc=soc, bat=.5 + 1.4 * soc + rng.normal(0, .1), set={}))
        self.assertGreater(np.corrcoef([m['ghz'] for m in rows], [m['load'] for m in rows])[0, 1], .6)
        model = report.fit_rows(report.battery_rows(rows))
        self.assertAlmostEqual(model['clock'], 2, delta=.2)
        self.assertAlmostEqual(model['load'], .2, delta=.02)

    def test_each_profiles_idle_clock_carries_the_clock_term_across_profiles(self):
        # Fitted on 85% power-saver minutes, the raw mean clock over-predicted balanced ones.
        train, test = chip_minutes(1200, 5), chip_minutes(600, 6)
        model = report.fit_rows(report.battery_rows(train))
        self.assertGreater(model['idle_ghz:balanced'] - model['idle_ghz:low-power'], .4)
        raw = {k: v for k, v in model.items() if not k.startswith('idle_ghz:')}  # One idle clock for all
        self.assertGreater(chip_error(raw, test, 'balanced').mean(), 1.0)
        self.assertLess(abs(chip_error(model, test, 'balanced').mean()), .25)
        self.assertLess(abs(chip_error(model, test, 'low-power').mean()), .15)
        # Unknown profile: the pooled idle clock, as before.
        self.assertEqual(report.clock_of(dict(test[0], set={}), model), test[0]['ghz'])

    def test_the_active_cores_clock_is_used_once_minutes_log_it(self):
        train, test = chip_minutes(1200, 7, active=True), chip_minutes(600, 8, active=True)
        without = [{k: v for k, v in m.items() if k != 'ghz_active'} for m in train]
        mean_model, active_model = report.fit_rows(without), report.fit_rows(train)
        self.assertNotIn('idle_ghz_active', mean_model)
        self.assertEqual(active_model['active_rows'], 1200)
        self.assertNotIn('idle_ghz_active', report.fit_rows(train[:100] + without[100:]))  # Too few yet
        stripped = [dict(m, ghz_active=None) for m in test]
        for profile in ('balanced', 'low-power'):
            self.assertLess(np.abs(chip_error(active_model, test, profile)).mean(),
                            0.7 * np.abs(chip_error(mean_model, stripped, profile)).mean())
        self.assertAlmostEqual(active_model['clock_active'], .9, delta=.15)


class AttributionTests(unittest.TestCase):
    MODEL = dict(report.PRIOR, floor=2.5, load=.15, clock=3.0, idle_ghz=1.0, chip=1.4, bl=3.0,
                 **{'idle_ghz:low-power': 1.0, 'idle_ghz:balanced': 1.5, 'idle_ghz:balanced:ac': 1.9})

    def steady(self, mhz, status='Discharging', profile='balanced', active=None, bat=20.0):
        records = []
        for i in range(6):
            r = record(T0 + 10 * (i + 1) - 5, bat=bat, soc=8.0, busy=3.0, mhz=mhz, status=status, bl=.5,
                       apps={'Floorp': {'cpu': 12.0, 'gpu': 1.0}, 'yosys': {'cpu': 6.0}})
            r['settings'] = {'platform_profile': profile}
            r['net'] = {'wlan0': {'rx': 2e6, 'tx': 1e6}}
            if active:
                r['cpu']['mhz_active'] = active
            records.append(r)
        (m,), _ = store.aggregate(records)
        if active:
            m['ghz_active'] = active / 1000  # store layout 9 (the data agent's) keeps this per minute.
        return records, m

    def assert_agree(self, records, m, model):
        per_record = collections.defaultdict(float)
        for r in records:
            for name, joules in power.attribute(r, model).items():
                per_record[name] += joules / sum(r['dt'] for r in records)
        parts = report.attribute(m, model)
        for name, watts in parts['apps'].items():
            self.assertAlmostEqual(per_record[name], watts, places=6, msg=name)
        self.assertAlmostEqual(per_record['Processor baseline'], parts['groups']['Processor baseline'], places=6)
        self.assertAlmostEqual(per_record.get('Display', 0), parts['groups'].get('Display', 0), places=6)
        self.assertAlmostEqual(sum(per_record.values()), sum(parts['groups'].values()), places=6)
        return parts

    def test_the_collector_and_the_panel_split_a_steady_minute_alike(self):
        model = dict(self.MODEL, **{'idle_ghz_active': 1.8, 'clock_active': 0.9})
        for mhz, status, profile, active in ((2400, 'Discharging', 'balanced', None),
                                             (2400, 'Charging', 'balanced', None),
                                             (900, 'Discharging', 'low-power', None),
                                             (2400, 'Discharging', None, None),
                                             (2400, 'Discharging', 'balanced', 3100)):
            with self.subTest(mhz=mhz, status=status, profile=profile, active=active):
                self.assert_agree(*self.steady(mhz, status, profile, active), model)

    def test_the_clock_above_the_profiles_idle_goes_to_apps_by_cpu_share(self):
        records, m = self.steady(1500)  # Balanced idles at 1.5 GHz: all of it baseline.
        parts = report.attribute(m, self.MODEL)
        self.assertAlmostEqual(parts['groups']['Processor baseline'], 1.4 * (2.5 + 3.0 * 1.0))
        low = report.attribute(dict(m, set={'profile': 'low-power'}), self.MODEL)  # 0.5 GHz above its idle
        self.assertAlmostEqual(low['groups']['Processor baseline'], parts['groups']['Processor baseline'])
        self.assertAlmostEqual(sum(low['apps'].values()), 1.4 * (8.0 - 5.5))  # The rest of the chip: apps'
        self.assertGreater(low['apps']['yosys'], parts['apps']['yosys'])  # Its CPU share of the raised clock

    def test_the_stack_never_exceeds_the_battery_draw(self):
        _, m = self.steady(2400, bat=9.0)  # Less than k × chip (1.4 × 8 W)
        parts = report.attribute(m, self.MODEL)
        self.assertAlmostEqual(sum(parts['groups'].values()), 9.0)
        self.assertEqual(parts['devices'], {})
        (point,) = report.timeline([m], self.MODEL)
        self.assertAlmostEqual(sum(point['groups'].values()), 9.0)



def setting_minutes(blocks, seed, saves=None, names=('aspm', 'apst', 'wifi-ps', 'abm', 'refresh'), runs=4,
                    sigma=1.0, pair=None):
    """Combined runs randomizing `names` per 4-minute block (first minute washout), with
    the W each setting's saving state saves (`saves`) and a run baseline each."""
    rng, minutes, saves = np.random.default_rng(seed), [], saves or {}
    for block in range(blocks):
        run = f'auto-{block * runs // blocks}'
        config = {n: bool(rng.integers(2)) for n in names}
        bat = (15 + (block * runs // blocks) * 0.7 + rng.normal(0, sigma)
               - sum(saves.get(n, 0) * on for n, on in config.items()))
        if pair:
            bat -= pair[1] * all(config[n] for n in pair[0])
        for i, m in enumerate(block_minutes(T0 + 240 * block, config, block, run=run, bat=0)):
            m['bat'] = bat + rng.normal(0, .3)
            m['exp'][1], m['exp'][5] = '+'.join(names), None
            m['set'] = dict(m['set'], abm=3 if config.get('abm') else 0)
            minutes.append(m)
    return minutes


class SelectionTests(unittest.TestCase):
    def test_without_any_effect_little_is_recommended_and_the_interval_mostly_holds_zero(self):
        # Picking the lowest of 2^n point predictions reported ~2 W saved from nothing.
        results = []
        for seed in range(8):
            levers = report.lever_effects(setting_minutes(160, seed), 50, 15, replicates=80)
            levers['current'] = dict.fromkeys(levers['current'], False)
            best = report.best_config(levers, set(report.SETTING))
            results.append(best)
            for name in best['changes']:
                self.assertGreaterEqual(best['chance'][name], report.CONFIDENCE)
        self.assertLess(np.mean([b['watts'] for b in results]), .4)
        self.assertLessEqual(sum(b['watts_low'] > 0 for b in results), 2)

    def test_settings_that_surely_save_are_chosen_and_one_that_costs_never(self):
        saves = {'aspm': 2.0, 'apst': 0.0, 'wifi-ps': 1.0, 'abm': -1.0, 'refresh': 0.0}
        levers = report.lever_effects(setting_minutes(240, 12, saves), 50, 15, replicates=80)
        levers['current'] = dict.fromkeys(levers['current'], False)
        best = report.best_config(levers, set(saves), 50, 15)
        self.assertTrue(best['config']['aspm'] and best['config']['wifi-ps'])
        self.assertFalse(best['config']['abm'])  # Its saving state costs
        self.assertFalse(best['config']['apst'] or best['config']['refresh'])  # Nothing to gain
        self.assertAlmostEqual(best['watts'], 3.0, delta=.6)
        self.assertLess(best['watts_low'], best['watts'])
        excluded = report.best_config(levers, set(saves), exclude=('aspm',))
        self.assertNotIn('aspm', excluded['config'])
        self.assertEqual(excluded['changes'], ['wifi-ps'])
        # ABM on (yours) and costing: turning it off is the saving.
        levers['current']['abm'] = True
        self.assertIn('abm', report.best_config(levers, {'abm'})['changes'])
        recs = report.recommendations(levers, report.qol_model([], []), 50, 15, exclude=('aspm', 'wifi-ps'))
        self.assertEqual(recs['battery']['best']['changes'], ['abm'])

    def test_only_prespecified_pairs_take_part_and_others_need_a_false_discovery_rate(self):
        result = report.lever_effects(factorial_minutes(), energy=50, power=15, replicates=100)
        for i in result['interactions']:
            self.assertIn('p', i)
            self.assertFalse(i['prespecified'])
        clear = {frozenset(i['pair']) for i in result['interactions'] if i['clear']}
        self.assertEqual(clear, {frozenset({'aspm', 'apst'})})  # The real one; boost's pairs are noise
        terms, factor, _, _ = report.selection(result)
        self.assertEqual(terms, ['aspm', 'apst', 'boost'])
        self.assertTrue(all(0 < f <= 1 for f in factor))


class PlanNoiseTests(unittest.TestCase):
    def test_block_noise_is_after_each_runs_baseline_and_plan_uses_it(self):
        minutes = setting_minutes(160, 2, {'aspm': 2.0}, runs=8, sigma=1.0)  # Runs 0.7 W apart
        blocks = report.lever_blocks(minutes)
        sigma = report.block_sigma(blocks)
        self.assertAlmostEqual(sigma, 1.0, delta=.2)
        x = np.array([[1.0, *(b[c] for c in report.COVARIATES)] for b in blocks])
        y = np.array([b['bat'] for b in blocks])
        alone = np.std(y - x @ np.linalg.lstsq(x, y, rcond=None)[0])  # The workload alone, as before
        self.assertGreater(alone, 1.5 * sigma)
        plan = report.plan(minutes, [], ['wifi-ps', 'refresh'], present=False, rng=np.random.default_rng(0))
        self.assertAlmostEqual(plan['sigma'], sigma)  # The settings outside the plan (ASPM's 2 W) modelled too

    def test_cpu_settings_are_planned_with_their_load_saving(self):
        # Boost off saves through a lower clock, i.e. lower load: adjusting for
        # load would see nothing to gain from it.
        rng, minutes = np.random.default_rng(6), []
        for block in range(50):
            config = {'boost': bool(rng.integers(2)), 'apst': bool(rng.integers(2))}
            load = max(.2, rng.normal(6, 1)) * (0.5 if config['boost'] else 1.0)
            for m in block_minutes(T0 + 240 * block, config, block, bat=0):
                m.update(load=load, bat=12 + .8 * load + rng.normal(0, .3))
                minutes.append(m)
        plans = [report.plan(minutes, [], ['boost', 'apst'], present=False, rng=np.random.default_rng(seed),
                             group=report.block_group('auto-1', minutes[-1]['t'])) for seed in range(30)]
        self.assertLess(sum(not p['choice']['boost'] for p in plans) / len(plans), .25)


class BootTests(unittest.TestCase):
    def boots(self, on, off, seed=5, effect=1.5):
        rng, minutes, t = np.random.default_rng(seed), [], T0
        for i, psr in enumerate([True] * on + [False] * off):
            base, balanced = rng.normal(0, .5), (0.6 if psr else 0.1)  # PSR boots ran balanced more
            for _ in range(60):
                load, profile = rng.uniform(1, 20), 'balanced' if rng.random() < balanced else 'low-power'
                minutes.append(minute(t, load=load, soc=5 + .2 * load, set={'psr': psr, 'profile': profile},
                                      bat=9 + .3 * load + base + 2.0 * (profile == 'balanced') - effect * psr
                                      + rng.normal(0, .5), boot=f'b{i}'))
                t += 60
        return minutes

    def test_boot_effect_needs_five_boots_each_and_adjusts_for_the_profile(self):
        progress = report.boot_effect(self.boots(4, 6), 48, 16)
        self.assertNotIn('watts', progress)
        self.assertEqual(progress['boots'], {'on': 4, 'off': 6})
        effect = report.boot_effect(self.boots(6, 6), 48, 16)
        self.assertIn('profile', effect['adjusted'])
        self.assertAlmostEqual(effect['watts'], 1.5, delta=.5)  # Not 1.5 − 2 W × (0.6 − 0.1)
        self.assertLess(effect['watts_low'], effect['watts'])
        self.assertGreater(effect['minutes'], 0)

    def test_the_boot_interval_is_welchs_over_the_boot_baselines(self):
        # Without noise inside boots the fit returns each boot's baseline exactly; the
        # interval is then Welch's t over them, with its own degrees of freedom.
        on, off = [0.2, -0.4, 0.5, 0.1, -0.3], [0.6, 0.0, -0.2, 0.9, 0.3, -0.5, 1.4]
        minutes, t = [], T0
        for i, (psr, base) in enumerate([(True, b) for b in on] + [(False, b) for b in off]):
            for j in range(60):
                load = 1 + (7 * j + i) % 19
                minutes.append(minute(t, load=load, set={'psr': psr, 'profile': 'low-power'},
                                      bat=9 + .3 * load + base - 1.5 * psr, boot=f'b{i}'))
                t += 60
        effect = report.boot_effect(minutes, 48, 16)
        a, b = np.array(on) + 9 - 1.5, np.array(off) + 9
        va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
        df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
        half = scipy.stats.t.ppf(0.95, df) * math.sqrt(va + vb)
        self.assertAlmostEqual(effect['watts'], b.mean() - a.mean(), places=6)
        self.assertAlmostEqual(effect['df'], df, places=6)
        self.assertAlmostEqual(effect['watts_low'], b.mean() - a.mean() - half, places=6)
        self.assertAlmostEqual(effect['watts_high'], b.mean() - a.mean() + half, places=6)

    def test_boot_interval_covers_about_ninety_percent(self):
        hits = sum(e['watts_low'] <= 1.5 <= e['watts_high']
                   for e in (report.boot_effect(self.boots(5, 5, seed), 48, 16) for seed in range(40)))
        self.assertGreaterEqual(hits, 32)  # 90% of 40 is 36; the old one held 69%


class SleepTests(unittest.TestCase):
    def gap(self, t1, hours, drop, **extra):
        return {'event': 'gap', 't1': t1, 'gap_s': hours * 3600, **extra,
                'bat_before': {'pct': 60, 'charge': 2_000_000, 'volts': 15.5, 'unit': 'uAh'},
                'bat_after': {'pct': 58, 'charge': 2_000_000 - drop}}

    def test_only_real_suspends_on_battery_count(self):
        t = T0 + 100 * 3600
        events = [self.gap(t, 2, 120_000),                                       # 0.93 W
                  self.gap(t + 10 * 3600, 0.7, 350_000),                         # 7.8 W: awake
                  self.gap(t + 20 * 3600, 2, 120_000, suspended_s=2 * 3600 - 60),  # Suspended throughout
                  self.gap(t + 30 * 3600, 2, 120_000, suspended_s=1800),          # Mostly awake
                  self.gap(t + 40 * 3600, 2, 120_000)]                            # Plugged in after
        self.assertEqual(len(report.sleep_drain(events)), 3)
        around = lambda t1, hours, st: [minute(store.minute_of(t1 - hours * 3600) - 60, st='D'),
                                        minute(store.minute_of(t1), st=st)]
        minutes = sorted(around(t, 2, 'D') + around(t + 20 * 3600, 2, 'D') + around(t + 40 * 3600, 2, 'C'),
                         key=lambda m: m['t'])
        self.assertEqual([s['end'] for s in report.sleep_drain(events, minutes)], [t, t + 20 * 3600])

    def test_one_odd_suspend_moves_the_sleep_model_little(self):
        sleeps = [{'watts': w, 'hours': h} for w, h in ((0.9, 3), (1.0, 1), (1.1, 2), (0.95, 8), (1.05, 0.5))]
        centre, spread, _ = report.sleep_model(sleeps)
        odd = report.sleep_model(sleeps + [{'watts': 2.9, 'hours': 6}])
        self.assertAlmostEqual(math.exp(odd[0]), math.exp(centre), delta=.06)
        self.assertLess(odd[1], 2 * spread)
        wide = report.sleep_model([dict(s, watts=s['watts'] ** 3) for s in sleeps])  # Three times the spread
        self.assertGreater(wide[1], 2 * spread)


class CleanPartsTests(unittest.TestCase):
    def test_blocks_keep_their_clean_records_when_the_minute_has_washout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'power.sqlite3'
            writer, records = store.Writer(db), []
            for block in range(4):  # Blocks start 30 s into a minute: its first records are washout.
                start = T0 + 240 * block + 30
                exp = {'run': 'r', 'name': 'aspm', 'block': block, 'arm': 'AB'[block % 2],
                       'value': ('performance', 'powersupersave')[block % 2]}
                for i in range(24):
                    records.append(record(start + 10 * (i + 1), bat=16 - 2 * (block % 2),
                                          exp=dict(exp, washout=i < 3)))
            for r in records:
                writer.write(r)
            writer.close()
            minutes, _ = report.load_range(T0 - 60, T0 + 1200, db)
            plain, _ = report.load_range(T0 - 60, T0 + 1200, db, apps=False)
        self.assertNotIn('apps', plain[0])
        (second,) = [m for m in minutes if m['t'] == T0 + 60]  # 1 washout record, 5 clean
        self.assertTrue(second['exp'][4])
        self.assertEqual([part['dt'] for part in second['clean']], [50])
        blocks = report.experiment_blocks(minutes)
        self.assertEqual({round(b['seconds']) for b in blocks.values()}, {210})  # 21 clean records each, not 180
        self.assertEqual({b['watts'] for b in blocks.values()}, {16, 14})

    def test_a_store_without_the_apps_switch_still_loads(self):
        def old_load_range(start, end, db):
            return [minute(T0)], []
        with mock.patch.object(store, 'load_range', old_load_range):
            minutes, _ = report.load_range(0, 1, '/nonexistent.sqlite3', apps=False)
        self.assertEqual(len(minutes), 1)


class SmallerFixesTests(unittest.TestCase):
    def test_a_setting_is_measured_with_what_it_does_to_the_disk(self):
        # ASPM saves 2 W but its slower link keeps the disk busier, which costs 1 W.
        rng, minutes = np.random.default_rng(9), []
        for block in range(160):
            config = {'aspm': bool(rng.integers(2)), 'wifi-ps': bool(rng.integers(2))}
            disk = 0.1 + 0.5 * config['aspm'] + rng.uniform(0, .05)
            bat = 15 - 2.0 * config['aspm'] + 2.0 * disk + rng.normal(0, .5)
            for m in block_minutes(T0 + 240 * block, config, block, bat=0):
                m.update(disk=disk, bat=bat + rng.normal(0, .2))
                minutes.append(m)
        result = report.lever_effects(minutes, 50, 15, replicates=60)
        result['current'] = dict.fromkeys(result['current'], False)
        (aspm,) = [e for e in result['effects'] if e['experiment'] == 'aspm']
        self.assertAlmostEqual(aspm['watts'], 1.0, delta=.3)

    def test_few_pairs_get_a_t_interval(self):
        minutes = []
        for block, watts in enumerate((16, 14.5, 16, 13.5, 16, 14.0)):
            arm = 'AB'[block % 2]
            minutes += [{'t': T0 + block * 1000 + i * 60, 'dt': 60, 'st': 'D', 'bat': watts,
                         'exp': ['r', 'freeze-apps', block, arm, False, ('running', 'frozen')[block % 2]]}
                        for i in range(3)]
        (effect,) = report.experiment_effects(minutes, 48, 16)
        deltas = np.array([1.5, 2.5, 2.0])
        half = 2.920 * deltas.std(ddof=1) / math.sqrt(3)  # Student's t with 2 degrees of freedom, 95% one-sided
        self.assertAlmostEqual(effect['watts_low'], 2.0 - half, places=3)
        self.assertAlmostEqual(effect['watts_high'], 2.0 + half, places=3)

    def test_runtime_comes_from_the_energy_above_the_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery = Path(tmp) / 'class/power_supply/BAT1'
            battery.mkdir(parents=True)
            (battery / 'energy_full').write_text('55000000\n')
            self.assertAlmostEqual(report.full_energy([], Path(tmp)), 55.0)
            self.assertAlmostEqual(report.full_energy([], Path(tmp), reserve_wh=3.3), 51.7)

    def test_each_vote_counts_once_in_its_own_window(self):
        # A block in the middle of a 4-minute window, between minutes outside experiments.
        minutes = [dict(m, qol=0) for m in block_minutes(T0 + 120, {'aspm': True, 'apst': False}, 1)]
        minutes = ([minute(T0 + 60 * i, qol=0, present=1.0, set={}) for i in range(2)] + minutes
                   + [minute(T0 + 360 + 60 * i, qol=0, present=1.0, set={}) for i in range(2)])
        events = [{'event': 'qol', 't1': T0 + 200, 'vote': 'down'}]
        windows = report.qol_windows(minutes, events)
        self.assertEqual(sum(w[1]['down'] for w in windows), 1)
        (voted,) = [w for w in windows if w[1]['down']]
        self.assertTrue(voted[2] and voted[0]['aspm'])  # In the experiment block's window
        model = report.qol_model(minutes, events)['down']
        self.assertIn('experiment', model)
        self.assertLess(report.qol_risk({'down': model}, {}, experiment=False),
                        report.qol_risk({'down': model}, {}, experiment=True) + 1e-9)


if __name__ == '__main__':
    unittest.main()
