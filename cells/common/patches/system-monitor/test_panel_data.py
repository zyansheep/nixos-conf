import io
import time
import unittest
from unittest import mock

import numpy as np

import eta
import experiment
import panel_data
import report

LEVERS = ['aspm', 'apst', 'wifi-ps', 'abm', 'refresh', 'boost', 'profile', 'gpu-dpm', 'cpu-cap', 'throttle-apps']
# Each setting's (saving, normal) value as the runner sets it, and as a minute logs it.
VALUES = {'aspm': ('powersupersave', 'performance'), 'apst': ('100000', '0'), 'wifi-ps': ('on', 'off'),
          'abm': ('3', '0'), 'refresh': ('2256x1504@47.998', '2256x1504@59.999'), 'boost': ('0', '1'),
          'profile': ('power-saver', 'balanced'), 'gpu-dpm': ('low', 'auto'), 'cpu-cap': ('2000', 'none'),
          'throttle-apps': ('on', 'off')}
SAVES = {'aspm': 2.0, 'apst': 0.3, 'wifi-ps': 0.1, 'abm': 0.5, 'refresh': 0.2, 'boost': 1.0, 'profile': 0.8,
         'gpu-dpm': 0.6, 'cpu-cap': 0.4, 'throttle-apps': 0.7}


def logged(config, psr):
    """A minute's `set` for {setting: in its saving state}."""
    on = lambda name: config.get(name, False)
    return {'aspm': VALUES['aspm'][0 if on('aspm') else 1], 'apst': VALUES['apst'][0 if on('apst') else 1],
            'wifi_ps': 'on' if on('wifi-ps') else 'off', 'abm': 3 if on('abm') else 0,
            'hz': 48.0 if on('refresh') else 60.0, 'boost': '1', 'profile': 'low-power' if on('profile') else 'balanced',
            'mhz': 2000.0 if on('cpu-cap') else (3501.0 if on('boost') else 4972.0),
            'cpu_mhz': 3501.0 if on('boost') else 4972.0, 'gpu_dpm': 'low' if on('gpu-dpm') else 'auto',
            'throttle': 'on' if on('throttle-apps') else 'off', 'psr': psr}


def month(days=30, seed=1, now=None):
    """A synthetic month of every kind of data the panel shows: battery and AC
    minutes, manual one-setting runs, app-freezing runs while locked, automatic
    runs of every setting (dict values, letter-string arms), boots with and
    without panel self-refresh, 👍/👎 votes and suspends."""
    rng, now = np.random.default_rng(seed), now or time.time()
    minutes, events = [], []
    for day in range(days):
        t = now - (days - day) * 86400
        boot, psr = f'b{day // 3}', (day // 3) % 2 == 1
        events.append({'event': 'start', 't1': t, 'boot': boot})
        kind = ('manual', 'freeze', 'auto', 'auto', 'none')[day % 5]
        run = f'{kind}-{day}'
        for block in range(90):  # 6 h on battery, in 4-minute blocks.
            config = {n: bool(rng.integers(2)) for n in LEVERS} if kind == 'auto' else {}
            if kind == 'manual':
                config = {'aspm': block % 2 == 1}
            arm = ''.join('B' if config[n] else 'A' for n in LEVERS) if kind == 'auto' else 'AB'[block % 2]
            value = ({n: VALUES[n][0 if config[n] else 1] for n in LEVERS} if kind == 'auto' else
                     VALUES['aspm'][0 if config.get('aspm') else 1] if kind == 'manual' else
                     'frozen' if block % 2 else 'running')
            load = max(0.2, rng.normal(4, 1.5))
            bat = (15 + 0.8 * load - sum(SAVES[n] for n, on in config.items() if on) - 1.5 * (kind == 'freeze' and block % 2)
                   + rng.normal(0, 0.8) - 0.7 * psr)
            for i in range(4):
                m = {'t': t, 'dt': 60, 'st': 'D', 'bat': bat + rng.normal(0, 0.5), 'soc': bat - 5, 'load': load,
                     'busy': load / 4, 'gpu': 0.01, 'video': 0.0, 'bl': 0.6, 'kbd': 0, 'wifi': 0.02, 'disk': 0.01,
                     'usb': 0, 'audio': 1 if block % 3 == 0 else 0, 'pct': 90 - block * 0.8, 'charge': 4e6 - block * 3e4,
                     'volts': 16.5 - block * 0.01, 'unit': 'uAh', 'boot': boot,
                     'present': 0.0 if kind == 'freeze' else 1.0, 'qol': 0,
                     'apps': {'Floorp': [load * 0.4, 0.01, 0], 'Kernel': [load * 0.1, 0, 0], 'yosys': [load * 0.3, 0, 0]},
                     'set': logged(config, psr)}
                if kind != 'none':
                    names = '+'.join(LEVERS) if kind == 'auto' else 'aspm' if kind == 'manual' else 'freeze-apps'
                    m['exp'] = [run, names, block, arm, i == 0, value]
                minutes.append(m)
                t += 60
            if kind == 'auto' and config['abm'] and block % 7 == 0:
                events.append({'event': 'qol', 't1': t - 90, 'vote': 'down'})
            if kind == 'auto' and not config['refresh'] and block % 9 == 0:
                events.append({'event': 'qol', 't1': t - 90, 'vote': 'up'})
        for i in range(120):  # 2 h charging.
            minutes.append({'t': t, 'dt': 60, 'st': 'C', 'bat': -30.0, 'soc': 8.0, 'load': 2.0, 'busy': 0.5,
                            'pct': 50 + i * 0.3, 'charge': 2e6 + i * 2e4, 'volts': 16.0, 'unit': 'uAh', 'boot': boot,
                            'present': 1.0, 'set': logged({}, psr)})
            t += 60
        events.append({'event': 'gap', 't0': t, 't1': t + 8 * 3600, 'gap_s': 8 * 3600, 'boot': boot,
                       'bat_before': {'pct': 86, 'charge': 4.4e6, 'volts': 16.6, 'unit': 'uAh'},
                       'bat_after': {'pct': 82, 'charge': 4.2e6, 'volts': 16.5, 'unit': 'uAh'}})
    return minutes, events


def history_of(minutes, events):
    load = lambda start, end: ([m for m in minutes if start <= m['t'] < end],
                               [e for e in events if start <= e.get('t1', 0) < end])
    return panel_data.History(load=load)


class FakeSpec:
    def __init__(self, name):
        self.name = name


SETTINGS = [{'name': n, 'label': n, 'description': '', 'root': False, 'visible': '', 'sleep_restored': False,
             'ac_managed': False, 'current': VALUES[n][1], 'alternative': VALUES[n][0], 'saving': VALUES[n][0],
             'normal': VALUES[n][1], 'error': None} for n in LEVERS]


def hermetic():
    """No sysfs, subprocesses or real models: the panel's view of the machine, fixed."""
    return [mock.patch.object(experiment, 'pool', lambda: list(LEVERS)),
            mock.patch.object(experiment, 'goal', lambda: 'battery'),
            mock.patch.object(experiment, 'psr_arm', lambda: 'default'),
            mock.patch.object(experiment, 'catalog', lambda: {n: FakeSpec(n) for n in LEVERS}),
            mock.patch.object(experiment, 'describe', lambda spec: next(s for s in SETTINGS if s['name'] == spec.name)),
            mock.patch.object(experiment, 'freezable_units', lambda: []),
            mock.patch.object(eta, 'load_models', lambda: None)]


class GatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.minutes, cls.events = month(days=5)  # Every kind of data; the budget test takes a whole month.

    def setUp(self):
        for patch in hermetic():
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_part_of_a_month_of_every_kind_of_data_loads_within_the_budget(self):
        now = time.time()
        history = history_of(*month())
        for name, length in panel_data.PERIODS.items():
            result = panel_data.gather(now - length, now, history)
            self.assertEqual(result['errors'], {}, name)
            # Twice the panel's budget, for a busy build machine: this month is ~10× the blocks of the real one,
            # and the load this guards against took 20 s.
            self.assertLess(result['seconds'], 2 * panel_data.BUDGET, (name, result['timings']))
        self.assertTrue(result['points'])
        self.assertEqual({e['experiment'] for e in result['levers']['effects']}, set(LEVERS))
        aspm = next(e for e in result['levers']['effects'] if e['experiment'] == 'aspm')
        self.assertAlmostEqual(aspm['watts'], SAVES['aspm'], delta=0.6)
        self.assertEqual([e['experiment'] for e in result['effects']], ['freeze-apps'])  # The paired A/B analysis.
        self.assertIsNotNone(result['plan'])
        self.assertIn('watts', result['psr'])
        self.assertTrue(result['sleeps_all'])
        recs = report.recommendations(result['levers'], result['plan']['qol'], result['energy'], result['typical'])
        self.assertTrue(recs['battery']['apply'])

    def test_the_period_comes_first_and_the_experiments_are_reused_until_the_history_changes(self):
        now, history, partials = time.time(), history_of(self.minutes, self.events), []
        first = panel_data.gather(now - 86400, now, history, on_period=partials.append)
        self.assertTrue(partials[0]['pending'] and partials[0]['points'])
        self.assertNotIn('levers', partials[0])
        with mock.patch.object(report, 'lever_effects', side_effect=AssertionError('refitted')):
            other = panel_data.gather(now - 7 * 86400, now, history)  # Another period, same history.
        self.assertEqual(other['errors'], {})
        self.assertIs(other['levers'], first['levers'])
        self.minutes.append(dict(self.minutes[-1], t=self.minutes[-1]['t'] + 60))  # A new minute arrives.
        self.addCleanup(self.minutes.pop)
        with mock.patch.object(report, 'lever_effects', return_value=panel_data.no_levers()) as refit:
            panel_data.gather(now - 86400, now + 120, history)
        refit.assert_called_once()

    def test_a_part_that_fails_is_reported_and_the_rest_still_shows(self):
        now = time.time()
        with mock.patch.object(report, 'lever_effects', side_effect=TypeError('unhashable type')), \
                mock.patch('sys.stderr', io.StringIO()) as stderr:
            result = panel_data.gather(now - 86400, now, history_of(self.minutes, self.events))
        self.assertEqual(list(result['errors']), ['settings model'])
        self.assertIn('TypeError', result['errors']['settings model'])
        self.assertIn('Traceback', stderr.getvalue())  # For the journal.
        self.assertTrue(result['points'])
        self.assertEqual(result['levers'], panel_data.no_levers())
        self.assertIsNotNone(result['plan'])

    def test_an_unreadable_database_says_so(self):
        broken = panel_data.History(load=mock.Mock(side_effect=OSError('database is locked')))
        with mock.patch('sys.stderr', io.StringIO()):
            result = panel_data.gather(0, 1, broken)
        self.assertIn('database is locked', result['error'])

    def test_no_data_at_all_still_loads(self):
        result = panel_data.gather(time.time() - 86400, time.time(), history_of([], []))
        self.assertEqual(result['errors'], {})
        self.assertEqual((result['points'], result['levers'], result['plan']['allowed'] > 0), ([], result['levers'], True))

    def test_the_check_reports_failures(self):
        out = io.StringIO()
        history = history_of(self.minutes, self.events)
        self.assertEqual(panel_data.check(out, budget=60, history=history), 0)
        self.assertEqual(panel_data.check(io.StringIO(), budget=0, history=history), 1)  # Too slow fails too.
        with mock.patch.object(report, 'boot_effect', side_effect=ValueError('x')), mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(panel_data.check(out, budget=60, history=history_of(self.minutes, self.events)), 1)
        self.assertIn('panel self-refresh FAILED: ValueError: x', out.getvalue())


class HistoryTests(unittest.TestCase):
    def test_a_reload_fetches_only_from_the_newest_minute_and_keeps_events_once(self):
        calls, now = [], [1_000_000.0]
        data = {'minutes': [{'t': 999_000.0}], 'events': [{'event': 'qol', 't1': 999_100.0}]}

        def load(start, end):
            calls.append(start)
            return ([m for m in data['minutes'] if start <= m['t'] < end],
                    [e for e in data['events'] if start <= e['t1'] < end])
        history = panel_data.History(load=load, clock=lambda: now[0])
        self.assertEqual(len(history.get()[0]), 1)
        data['minutes'].append({'t': 999_060.0})
        now[0] += 60
        minutes, events = history.get()
        self.assertEqual(calls[-1], 999_000.0)
        self.assertEqual([m['t'] for m in minutes], [999_000.0, 999_060.0])
        self.assertEqual(len(events), 1)


class ExperimentEffectsTests(unittest.TestCase):
    def test_combined_runs_are_left_to_the_setting_model(self):
        minutes, _ = month(days=5)
        effects = report.experiment_effects(minutes, 50, 15)
        self.assertEqual(sorted(e['experiment'] for e in effects), ['aspm', 'freeze-apps'])


if __name__ == '__main__':
    unittest.main()
