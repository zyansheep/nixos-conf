import contextlib
import functools
import inspect
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import eta
import experiment
import panel_data
import report
import store

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


KINDS = ('manual', 'freeze', 'auto', 'auto', 'none')  # 'none': normal battery time, no experiment.


def month(days=30, seed=1, now=None, kinds=KINDS):
    """A synthetic month of every kind of data the panel shows: battery and AC
    minutes, manual one-setting runs, app-freezing runs while locked, automatic
    runs of every setting (dict values, letter-string arms), boots with and
    without panel self-refresh, 👍/👎 votes and suspends (each night, from
    battery to battery: sleep_drain leaves out one that touches AC)."""
    rng, now = np.random.default_rng(seed), now or time.time()
    minutes, events = [], []

    def charging(t, boot, psr):
        for i in range(120):  # 2 h charging.
            minutes.append({'t': t, 'dt': 60, 'st': 'C', 'bat': -30.0, 'soc': 8.0, 'load': 2.0, 'busy': 0.5, 'ghz': 1.4,
                            'pct': 50 + i * 0.3, 'charge': 2e6 + i * 2e4, 'volts': 16.0, 'unit': 'uAh', 'boot': boot,
                            'present': 1.0, 'set': logged({}, psr)})
            t += 60
        return t
    for day in range(days):
        t = now - (days - day) * 86400
        boot, psr = f'b{day // 3}', (day // 3) % 2 == 1
        events.append({'event': 'start', 't1': t, 'boot': boot})
        kind = kinds[day % len(kinds)]
        run = f'{kind}-{day}'
        for block in range(90):  # 6 h on battery, in 4-minute blocks, with 2 h of charging midway.
            if block == 45:
                t = charging(t, boot, psr)
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
                m = {'t': t, 'dt': 60, 'st': 'D', 'bat': bat + rng.normal(0, 0.5), 'soc': bat - 5, 'load': load, 'ghz': 1 + load / 8,
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
        wake = now - (days - day - 1) * 86400  # Asleep until the next day's first minute.
        events.append({'event': 'gap', 't0': t, 't1': wake, 'gap_s': wake - t, 'boot': boot,
                       'bat_before': {'pct': 86, 'charge': 4.4e6, 'volts': 16.6, 'unit': 'uAh'},
                       'bat_after': {'pct': 82, 'charge': 4.2e6, 'volts': 16.5, 'unit': 'uAh'}})
    return minutes, events


def history_of(minutes, events):
    """A History over these minutes; like the store, without their apps when asked."""
    def load(start, end, apps=True):
        return ([m if apps else {k: v for k, v in m.items() if k != 'apps'} for m in minutes if start <= m['t'] < end],
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
    return [mock.patch.object(experiment, 'pool', lambda specs=None: list(LEVERS)),
            mock.patch.object(eta, 'reserve_wh', lambda: 0.0, create=True),
            mock.patch.object(experiment, 'goal', lambda: 'battery'),
            mock.patch.object(experiment, 'psr_arm', lambda: 'default'),
            mock.patch.object(experiment, 'catalog', lambda: {n: FakeSpec(n) for n in LEVERS}),
            mock.patch.object(experiment, 'describe', lambda spec: next(s for s in SETTINGS if s['name'] == spec.name)),
            mock.patch.object(experiment, 'freezable_units', lambda: []),
            mock.patch.object(experiment, 'preferences', lambda: {}),
            mock.patch.object(experiment, 'load_state', lambda: {}),
            mock.patch.object(experiment, 'RECOMMEND', Path('/nonexistent/recommend.json')),
            mock.patch.object(eta, 'MODELS', Path('/nonexistent/models.json')),
            mock.patch.object(eta, 'load_models', lambda: None),
            mock.patch.object(store, 'minutes_ready', lambda _db=None: True, create=True)]


def quick():
    """Fewer bootstrap replicates: for tests of what the panel shows, not of the statistics."""
    return [mock.patch.object(report, name, functools.partial(getattr(report, name), replicates=replicates))
            for name, replicates in (('lever_effects', 40), ('experiment_effects', 200), ('what_if', 40))]


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
        self.assertEqual(result['recs_source'], 'panel')  # No recommend.json here.
        self.assertTrue(result['recs']['battery']['apply'])

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
        self.assertEqual(panel_data.what_if_note(result), 'Not enough battery time for model estimates: they need 30 '
                                                          'minutes on battery, and the last 30 days have 0.')
        self.assertIn('Data: no battery minutes in the last 30 days, so no what-if; battery model: prior values',
                      panel_data.health_text(result['health']))
        self.assertEqual((result['points'], result['levers'], result['plan']['allowed'] > 0), ([], result['levers'], True))

    def test_the_check_reports_failures(self):
        out = io.StringIO()
        history = history_of(self.minutes, self.events)
        self.assertEqual(panel_data.check(out, budget=60, history=history), 0)
        self.assertEqual(panel_data.check(io.StringIO(), budget=0, history=history), 1)  # Too slow fails too.
        with mock.patch.object(report, 'boot_effect', side_effect=ValueError('x')), mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(panel_data.check(out, budget=60, history=history_of(self.minutes, self.events)), 1)
        self.assertIn('panel self-refresh FAILED: ValueError: x', out.getvalue())


class ScopeTests(unittest.TestCase):
    """What the what-if, the typical draw and the recommendations are computed
    on, and that the panel says so."""

    @classmethod
    def setUpClass(cls):
        cls.minutes, cls.events = month(days=5)  # The last day ('none') is the only battery time outside experiments.

    def setUp(self):
        for patch in hermetic() + quick() + [mock.patch.object(report, 'full_energy', lambda _minutes, **_: 50.0)]:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_history_is_loaded_without_apps_and_the_period_with_them(self):
        now, history = time.time(), history_of(self.minutes, self.events)
        result = panel_data.gather(now - 90000, now, history)  # The last day's 6 h on battery.
        self.assertFalse(any('apps' in m for m in history.minutes))
        self.assertTrue(any(p['apps'] for p in result['points']))
        self.assertTrue(any(e['name'].startswith('Close ') for e in result['estimates']))  # Apps to close.
        rows = [m for m in report.battery_rows(self.minutes) if m['t'] >= now - 90000]
        dates = panel_data.days(rows[0]['t'], rows[-1]['t'])
        self.assertEqual(result['scope'], f"this period's 6 h 00 m on battery ({dates})")
        self.assertIn(f"this period's 6 h 00 m on battery ({dates}), at your settings "
                      f"({result['info']['power']:.1f} W): the battery model", panel_data.what_if_note(result))

    def test_a_period_without_battery_time_borrows_the_latest_with_its_apps_and_says_so(self):
        now, history, calls = time.time(), history_of(self.minutes, self.events), []
        rows = report.battery_rows(self.minutes)  # Experiments' minutes too, but not their washout.
        latest = rows[-panel_data.FALLBACK_HOURS * 60:]
        tested = sum(1 for m in latest if m.get('exp'))
        self.assertTrue(0 < tested < len(latest))  # They reach back into the day before's experiments.
        load = history.load
        history.load = lambda start, end, apps=True: (calls.append((start, end, apps)), load(start, end, apps))[1]
        result = panel_data.gather(now - 6 * 3600, now, history)
        self.assertEqual(result['errors'], {})
        self.assertEqual(result['scope'], f"the latest 8 h 00 m on battery "
                                          f"({panel_data.days(latest[0]['t'], latest[-1]['t'])}), "
                                          f"{panel_data.duration(tested / 60)} of it in experiments "
                                          '(this period has under 30 minutes on battery)')
        self.assertTrue(any(e['name'].startswith('Close ') for e in result['estimates']))
        info = result['info']
        self.assertEqual((info['rows'], info['experiment_rows'], info['train_rows']), (len(latest), tested, len(rows)))
        self.assertIn(f", at your settings ({info['power']:.1f} W; experiments' settings, {info['settings_w']:+.1f} W "
                      'on average, taken out)', panel_data.what_if_note(result))
        all_tested = sum(1 for m in rows if m.get('exp'))
        self.assertEqual((result['health']['battery'], result['health']['tested'], result['health']['used']),
                         (len(rows), all_tested, len(latest)))
        text = panel_data.health_text(result['health'])
        self.assertIn(f'{len(rows):,} battery minutes in the last 30 days', text)
        self.assertIn(f'{all_tested:,} of them in experiments; the what-if used {len(latest)}', text)
        self.assertIn('battery model: on those minutes, fitted here: no models.json from battery-eta', text)
        calls.clear()  # The minute tick: only the newest borrowed minute is fetched again.
        panel_data.gather(now - 6 * 3600 + 60, now + 60, history)
        self.assertEqual([c for c in calls if c[2] and c[0] < now - 6 * 3600], [(rows[-1]['t'], rows[-1]['t'] + 60, True)])

    def test_the_typical_draw_is_at_your_settings_or_battery_etas(self):
        now, history = time.time(), history_of(self.minutes, self.events)
        mean = panel_data.gather(now - 86400, now, history)
        rows = report.battery_rows(self.minutes)
        self.assertAlmostEqual(mean['typical'], report.typical_power(rows, report.fit_rows(rows, minutes=self.minutes)))
        # The experiments' power-saving settings are taken out: above the plain mean, at the minutes outside them.
        outside = [m['bat'] for m in rows if not m.get('exp')]
        self.assertGreater(mean['typical'], sum(m['bat'] for m in rows) / len(rows) + 0.5)
        self.assertAlmostEqual(mean['typical'], sum(outside) / len(outside), delta=0.5)
        self.assertIn(f"mean at your settings of the last 30 days' {len(rows):,} battery minutes",
                      panel_data.typical_text(mean))
        shared = {'attribution': report.fit_rows(rows), 'fitted': now - 120, 'typical': 9.0, 'typical_power': 11.0}
        with mock.patch.object(eta, 'load_models', lambda: shared):
            result = panel_data.gather(now - 86400, now, history)
        self.assertEqual(result['typical'], 11.0)
        self.assertTrue(panel_data.typical_text(result).startswith(
            "Typical draw on battery 11.0 W (battery-eta's: the mean at your settings of the last 30 days on battery) → "
            '4.5 h per full charge (50 Wh usable)'))
        aspm = next(e for e in result['levers']['effects'] if e['experiment'] == 'aspm')
        self.assertAlmostEqual(aspm['minutes'], report.runtime_gain(50.0, 11.0, aspm['watts']))
        self.assertIn('battery model: on those minutes, trained', panel_data.health_text(result['health'], now))
        with mock.patch.object(eta, 'load_models', lambda: dict(shared, attribution=dict(report.PRIOR, prior=1.0))):
            result = panel_data.gather(now - 86400, now, history)
        self.assertIn(f'battery model: prior values (under {report.MIN_ROWS} battery minutes to fit), trained',
                      panel_data.health_text(result['health'], now))
        kept = dict(report.fit_rows(rows[:400]), kept=1.0)
        with mock.patch.object(eta, 'load_models', lambda: dict(shared, attribution=kept)):
            result = panel_data.gather(now - 86400, now, history)
        self.assertIn('battery model: its last fit, kept: 400 minutes (', panel_data.health_text(result['health'], now))
        with mock.patch.object(store, 'minutes_ready', lambda _db: False, create=True):
            result = panel_data.gather(now - 86400, now, history)
        self.assertTrue(panel_data.health_text(result['health']).startswith(
            'Data: the store is still recomputing older minutes after an update'))
        with tempfile.TemporaryDirectory() as directory:  # Just after an update: the old battery-eta's models.json.
            path = Path(directory) / 'models.json'
            path.write_text(json.dumps({'version': eta.MODELS_VERSION - 1, 'fitted': now - 60}))
            with mock.patch.object(eta, 'MODELS', path):
                result = panel_data.gather(now - 86400, now, history)
        self.assertIn("fitted here: models.json is from before an update (waiting for battery-eta's first refit)",
                      panel_data.health_text(result['health'], now))

    def test_suspends_that_touch_ac_are_left_out(self):
        now = time.time()
        t = next(m for m in self.minutes if m['st'] == 'C')['t'] + 600  # Plugged in on both sides.
        ac = {'event': 'gap', 't0': t, 't1': t + 3600, 'gap_s': 3600, 'boot': 'b0',
              'bat_before': {'pct': 60, 'charge': 3.0e6, 'volts': 16.0, 'unit': 'uAh'},
              'bat_after': {'pct': 59, 'charge': 2.98e6, 'volts': 16.0, 'unit': 'uAh'}}
        self.assertEqual(len(report.sleep_drain(self.events + [ac])), len(report.sleep_drain(self.events)) + 1)
        result = panel_data.gather(now - 7 * 86400, now, history_of(self.minutes, self.events + [ac]))
        self.assertNotIn(t, [s['start'] for s in result['sleeps_all'] + result['sleeps']])
        self.assertEqual(len(result['sleeps_all']), 4)  # Each night's, battery to battery.

    def test_a_period_that_starts_in_the_night_keeps_that_nights_suspend(self):
        now = time.time()
        start = now - 1.5 * 86400  # Asleep then, since the evening before.
        result = panel_data.gather(start, now, history_of(self.minutes, self.events))
        overlapping = [s['end'] for s in result['sleeps_all'] if s['end'] > start and s['start'] < now]
        self.assertEqual([s['end'] for s in result['sleeps']], overlapping)
        self.assertLess(result['sleeps'][0]['start'], start)

    def test_the_value_of_more_testing_in_words(self):
        value = {'horizon_days': 30, 'horizon_hours': 163.0, 'loss': 1.42, 'cost': 0.02, 'worth': True,
                 'rows': [{'hours': 10, 'loss': 1.36, 'saves': 8.8, 'costs': 0.2},
                          {'hours': 30, 'loss': 1.06, 'saves': 47.4, 'costs': 0.66},
                          {'hours': 100, 'loss': 0.74, 'saves': 42.6, 'costs': 2.2}]}
        text, rows = panel_data.value_text(value, typical=9.0)
        self.assertEqual(rows[1], ('30 more hours', 'saves 47 Wh (~5 h 16 m on battery)', 'costs 0.7 Wh'))
        self.assertTrue(text.startswith('More testing is worth it: 30 more hours of blocks would save 47 Wh'))
        self.assertIn('1.4 W more than the best combination', text)
        text, rows = panel_data.value_text(dict(value, worth=False, rows=[dict(value['rows'][0], saves=-.3, costs=-.1)]))
        self.assertEqual(rows, [('10 more hours', 'saves 0.0 Wh', 'costs nothing')])
        self.assertTrue(text.startswith('More testing would cost more than it saves'))
        retest = {'version': 'Linux 6.18, BIOS 3.20', 'tested': 9000}
        self.assertIn('New kernel or BIOS (Linux 6.18, BIOS 3.20): testing runs 10 hours first (2.5 done).',
                      panel_data.value_text(value, retest=retest)[0])
        self.assertTrue(panel_data.value_text(dict(value, rows=[]))[0].startswith('Too little battery time'))
        self.assertEqual(panel_data.value_text(None), ('', []))
        banner = panel_data.banner_text({'mode': 'auto', 'status': 'paused', 'settled': True, 'goal': 'comfort',
                                         'reason': experiment.SETTLED}, True)
        self.assertTrue(banner.startswith('Paused: more testing would cost more than it saves. Meanwhile the '
                                          'Comfort goal'))
        result = panel_data.gather(time.time() - 86400, time.time(), history_of(self.minutes, self.events))
        self.assertTrue(result['plan']['value']['rows'])  # The panel's own plan prices it too.

    def test_recommendations_leave_settings_outside_the_pool_as_they_are(self):
        now, pool = time.time(), [n for n in LEVERS if n not in ('aspm', 'abm')]
        with mock.patch.object(experiment, 'pool', lambda specs=None: pool):
            result = panel_data.gather(now - 86400, now, history_of(self.minutes, self.events))
        self.assertEqual(result['recs_source'], 'panel')
        self.assertFalse({a.partition('=')[0] for a in result['recs']['battery']['apply']} & {'aspm', 'abm'})

    def test_votes_count_without_a_pool_and_recommend_json_is_the_one_source(self):
        now, history = time.time(), history_of(self.minutes, self.events)
        with mock.patch.object(experiment, 'pool', lambda specs=None: []):
            result = panel_data.gather(now - 86400, now, history)
            unrecommended = experiment.unrecommended()
        self.assertIsNone(result['plan'])
        votes = report.qol_model(*history.get())
        self.assertEqual(result['quality']['down']['votes'], votes['down']['votes'])
        self.assertGreater(result['quality']['down']['votes'], 0)
        self.assertEqual(result['recs_source'], 'panel')
        self.assertEqual(set(unrecommended), set(report.SETTING))  # No pool: every setting is left as it is.
        expected = report.recommendations(result['levers'], votes, 50.0, result['typical'], exclude=unrecommended)
        self.assertEqual(result['recs']['battery']['apply'], expected['battery']['apply'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recommend.json'
            written = {'updated': now - 60, 'battery': {'text': 'ASPM on · +14 min', 'apply': ['aspm=saving']},
                       'comfort': {'text': 'same as Battery until a 👍', 'apply': ['aspm=saving']}}
            path.write_text(json.dumps(written))
            with mock.patch.object(experiment, 'RECOMMEND', path):
                fresh = panel_data.gather(now - 86400, now, history)
                path.write_text(json.dumps(dict(written, updated=now - 2 * 3600)))
                stale = panel_data.gather(now - 86400, now, history)
        self.assertEqual((fresh['recs_source'], fresh['recs']['battery']['apply']), ('battery-eta', ['aspm=saving']))
        self.assertEqual(stale['recs_source'], 'panel')

    def test_a_newer_reload_cancels_the_rest(self):
        now, stop, partials = time.time(), [False], []

        def on_period(partial):
            partials.append(partial)
            stop[0] = True
        with mock.patch.object(report, 'lever_effects', side_effect=AssertionError('fitted after the cancel')):
            result = panel_data.gather(now - 86400, now, history_of(self.minutes, self.events), on_period=on_period,
                                       cancelled=lambda: stop[0])
        self.assertIsNone(result)
        self.assertEqual(len(partials), 1)

    def test_the_what_if_is_computed_after_the_period_is_shown(self):
        now, order = time.time(), []
        what_if = report.what_if
        with mock.patch.object(report, 'what_if', lambda *a, **k: (order.append('what-if'), what_if(*a, **k))[1]):
            panel_data.gather(now - 86400, now, history_of(self.minutes, self.events),
                              on_period=lambda partial: order.append('period'))
        self.assertEqual(order, ['period', 'what-if'])


class ExperimentsOnlyTests(unittest.TestCase):
    """Thirty days in which every minute on battery ran an experiment (as since
    Oct 12, with automatic experiments on). The models hold settings at yours,
    so the what-if and the typical draw come from experiment minutes."""

    def setUp(self):
        for patch in hermetic() + quick() + [mock.patch.object(report, 'full_energy', lambda _minutes, **_: 50.0)]:
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_part_loads_from_experiment_minutes_and_says_so(self):
        now = time.time()
        minutes, events = month(kinds=('manual', 'freeze', 'auto', 'auto'))
        history = history_of(minutes, events)
        for name, length in panel_data.PERIODS.items():
            result = panel_data.gather(now - length, now, history)
            self.assertEqual(result['errors'], {}, name)
        rows = report.battery_rows(minutes)
        self.assertTrue(rows and all(m.get('exp') for m in rows))
        self.assertTrue(any(e['name'].startswith('Close ') for e in result['estimates']))
        info = result['info']
        self.assertEqual(info['experiment_rows'], info['rows'])
        amount = panel_data.duration(info['rows'] / 60)
        self.assertTrue(result['scope'].startswith(f"this period's {amount} on battery ("), result['scope'])
        self.assertTrue(result['scope'].endswith(f'{amount} of it in experiments'), result['scope'])
        self.assertIn(f"; experiments' settings, {info['settings_w']:+.1f} W on average, taken out)",
                      panel_data.what_if_note(result))
        self.assertAlmostEqual(result['typical'], report.typical_power(rows, report.fit_rows(rows, minutes=minutes)))
        self.assertIn('at your settings', panel_data.typical_text(result))
        self.assertIn(f'{len(rows):,} battery minutes in the last 30 days', panel_data.health_text(result['health']))
        self.assertIn(f'{len(rows):,} of them in experiments', panel_data.health_text(result['health']))
        self.assertEqual({e['experiment'] for e in result['levers']['effects']}, set(LEVERS))  # They still measure.
        self.assertTrue(result['recs']['battery']['apply'])
        shared = {'attribution': dict(report.PRIOR), 'fitted': now, 'typical': 9.0, 'typical_power': 12.0}
        with mock.patch.object(eta, 'load_models', lambda: shared):
            result = panel_data.gather(now - 86400, now, history)
        self.assertEqual(result['typical'], 12.0)  # battery-eta's when its models are fresh; never the ETA's level.
        self.assertTrue(all('minutes' in e for e in result['levers']['effects']))


class SQLiteBudgetTests(unittest.TestCase):
    """The panel's own loader on a month in SQLite, each minute listing ~40 apps
    as the real ones do: decoding them all on every open was 85% of its load."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.db = Path(cls.directory.name) / 'power.sqlite3'
        minutes, events = month()
        filler = {f'app{i}': [0.002 * i, 0.0, 0.0] for i in range(36)}
        with contextlib.closing(store.connect(cls.db)) as conn, store.transaction(conn):
            conn.executemany(store.INSERT_MINUTE, [store.minute_values(dict(m, apps={**m['apps'], **filler})
                                                                       if m.get('apps') else m) for m in minutes])
            conn.executemany(store.INSERT_EVENT, [(e.get('event'), e.get('t1', 0), e.get('t0'), e.get('boot'),
                                                   e.get('gap_s'), store.compact(e)) for e in events])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        for patch in hermetic() + quick() + [mock.patch.object(store, 'DB', self.db),
                                             mock.patch.object(report, 'full_energy', lambda _minutes, **_: 50.0)]:
            patch.start()
            self.addCleanup(patch.stop)

    def test_every_period_loads_within_the_budget_and_decodes_apps_only_for_it(self):
        now, history = time.time(), panel_data.History()
        for name, length in panel_data.PERIODS.items():
            result = panel_data.gather(now - length, now, history)
            self.assertEqual(result['errors'], {}, name)
            self.assertLess(result['seconds'], 2 * panel_data.BUDGET, (name, result['timings']))
        self.assertTrue(any(p['apps'] for p in result['points']))
        self.assertTrue(any(e['name'].startswith('Close ') for e in result['estimates']))
        if 'apps' in inspect.signature(store.load_range).parameters:
            self.assertFalse(any(m.get('apps') for m in history.minutes))


class TextTests(unittest.TestCase):
    NOW = 1_800_000_000.0
    DISCHARGING = {'status': 'Discharging', 'updated': NOW, 'pct': 52.0, 'energy_pct': 49, 'watts': 12.87,
                   'hours': 1.5907, 'low': 1.2619, 'high': 1.9817,
                   'sleep': {'hours': 23.29, 'low': 13.9, 'high': 39.04, 'watts': 0.954, 'suspends': 27}}

    def test_durations_round_to_the_minute_first(self):
        self.assertEqual([panel_data.duration(h) for h in (1.9925, 0.9999, 0.5, 25.5, 0.004, float('inf'))],
                         ['2 h 00 m', '1 h 00 m', '30 min', '25 h 30 m', '0 min', '--'])

    def test_time_left_is_awake_time_with_a_line_for_asleep(self):
        line = panel_data.status_line(self.DISCHARGING, {'pct': 52, 'status': 'Discharging', 'watts': 13.1}, self.NOW)
        self.assertEqual(line, '52% charge, 49% energy · discharging · 12.9 W · ~1 h 35 m left awake '
                               '(80%: 1 h 16 m–1 h 59 m)\nAsleep from now: ~23 h (80%: 14 h–39 h) at 1.0 W')

    def test_an_unknown_status_on_battery_is_battery_etas_discharging(self):
        unknown = dict(self.DISCHARGING, status='Unknown', classes=['fill49', 'discharging'])
        self.assertIn('discharging · 12.9 W · ~1 h 35 m left awake',
                      panel_data.status_line(unknown, {'pct': 52, 'status': 'Unknown'}, self.NOW))

    def test_a_plug_in_between_the_two_reads_follows_the_estimates_status(self):
        # sysfs says Charging a moment before eta.json does: its numbers are still the discharging ones.
        line = panel_data.status_line(self.DISCHARGING, {'pct': 52, 'status': 'Charging'}, self.NOW)
        self.assertIn('discharging · 12.9 W · ~1 h 35 m left awake', line)
        charging = {'status': 'Charging', 'updated': self.NOW, 'hours': 0.75, 'low': 0.5, 'high': 1.0, 'ratio': 1.44,
                    'ratio_low': 1.2, 'ratio_high': 1.71}  # No target: never a KeyError.
        self.assertEqual(panel_data.status_line(charging, {'pct': 60, 'status': 'Discharging', 'watts': -25.0}, self.NOW),
                         '60% charge · charging · 25.0 W · ×1.4 minutes of use per minute charging (80%: ×1.2–1.7) · '
                         '45 min to the limit (80%: 30 min–1 h 00 m)')
        self.assertIn('45 min to 80%', panel_data.status_line(dict(charging, target=80), {}, self.NOW))

    def test_the_last_minutes_count_down_to_the_power_off(self):
        clock = lambda t: time.strftime('%H:%M:%S', time.localtime(t))
        estimate = dict(self.DISCHARGING, hours=0.07, cutout=self.NOW + 250, cutout_pm=17.6, poweroff=self.NOW + 220)
        self.assertIn(f'powers off at {clock(self.NOW + 220)} (in 3m 40s, ±18 s; the battery cuts out at '
                      f'{clock(self.NOW + 250)})', panel_data.status_line(estimate, {}, self.NOW))
        del estimate['poweroff'], estimate['cutout_pm']
        self.assertIn(f'cuts out at {clock(self.NOW + 250)} (in 4m 10s)', panel_data.status_line(estimate, {}, self.NOW))

    def test_without_an_estimate_the_batterys_own_readings(self):
        self.assertEqual(panel_data.status_line(None, {'pct': 40, 'status': 'Discharging', 'watts': 10.0,
                                                       'charge_wh': 20.0}, self.NOW),
                         '40% · discharging · 10.0 W · ~2 h 00 m left awake at this draw')
        self.assertEqual(panel_data.status_line(None, {}, self.NOW), '')
        self.assertEqual(panel_data.status_line({'status': 'Not charging', 'updated': self.NOW}, {'pct': 80,
                                                'status': 'Not charging'}, self.NOW), '80% · not charging')

    def test_estimates_and_battery_readings_that_cant_be_read_are_left_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'eta.json'
            path.write_text(json.dumps(self.DISCHARGING))
            self.assertEqual(panel_data.read_estimate(path, self.NOW + 5)['hours'], 1.5907)
            self.assertIsNone(panel_data.read_estimate(path, self.NOW + 15))  # Stale.
            path.write_text('[1, 2]')
            self.assertIsNone(panel_data.read_estimate(path, self.NOW))
            self.assertIsNone(panel_data.read_estimate(root / 'missing.json', self.NOW))
            battery = root / 'BAT1'
            battery.mkdir()
            for name, value in (('status', 'Discharging\n'), ('capacity', '41\n'), ('current_now', '800000'),
                                ('voltage_now', 'garbage')):
                (battery / name).write_text(value)
            self.assertEqual(panel_data.read_battery(root), {'status': 'Discharging', 'pct': 41})
            (battery / 'voltage_now').write_text('15000000')
            (battery / 'charge_now').write_text('2000000')
            self.assertEqual(panel_data.read_battery(root), {'status': 'Discharging', 'pct': 41, 'watts': 12.0,
                                                             'charge_wh': 30.0})
            self.assertEqual(panel_data.read_battery(root / 'none'), {})

    def test_the_banner_survives_a_state_file_missing_keys(self):
        self.assertEqual(panel_data.banner_text({'status': 'running'}, True, self.NOW),
                         'Running: an experiment — block ?: ? (A = as before).')
        self.assertEqual(panel_data.banner_text({'mode': 'auto', 'status': 'running', 'block': 3,
                                                 'config': {'aspm': 'B', 'abm': 'A'}}, True, self.NOW),
                         'Block 3: ASPM on, ABM off.')
        self.assertEqual(panel_data.banner_text({}, False, self.NOW, quarantine={'aspm': 'sleep crash', 'apst': ''}),
                         'Off. Turn on to let the laptop test settings by itself while on battery.\nQuarantined after '
                         'a hardware incident (experiments leave them alone): aspm (sleep crash), apst.')

    def test_setting_rows(self):
        quality = report.qol_model(*month(days=5))
        levers = {'effects': [{'experiment': 'abm', 'watts': 0.5, 'watts_low': 0.2, 'watts_high': 0.8, 'minutes': 12.0,
                               'low': 5.0, 'high': 20.0, 'blocks': {'on': 40, 'off': 38}, 'hours': 5.1, 'chance': 0.97}],
                  'progress': {'wifi-ps': {'on': 2, 'off': 1}}}
        settings = [dict(s, error='no such device') if s['name'] == 'refresh' else s for s in SETTINGS]
        rows = {r['name']: r for r in panel_data.lever_rows(settings, levers, quality, {'apst', 'abm', 'gpu-dpm'},
                                                            risky=('aspm', 'gpu-dpm'),
                                                            quarantine={'apst': 'sleep crash'})}
        self.assertEqual([(rows[n]['included'], rows[n]['sensitive'])
                          for n in ('aspm', 'gpu-dpm', 'apst', 'abm', 'boost')],
                         [(False, False), (True, True), (False, False), (True, True), (False, True)])
        self.assertIn('risky: not in automatic runs', rows['aspm']['detail'])
        self.assertIn('pool add aspm --risky', rows['aspm']['include_hint'])
        # In the pool only because it was added with --risky: it runs, so it says so, and can be taken out.
        self.assertIn('risky: in automatic runs (added with --risky)', rows['gpu-dpm']['detail'])
        self.assertIn('Untick', rows['gpu-dpm']['include_hint'])
        self.assertIn('quarantined after a hardware incident (sleep crash)', rows['apst']['detail'])
        self.assertEqual((rows['abm']['value'], rows['abm']['evidence']), ('+12 min (+5 … +20)', '40+38 blocks, 5.1 h'))
        self.assertIn('97% chance it saves', rows['abm']['detail'])
        self.assertIn('👎 ', rows['abm']['detail'])  # The votes count, pool or not.
        self.assertEqual((rows['wifi-ps']['value'], rows['wifi-ps']['evidence']), ('needs more', '2+1 blocks so far'))
        self.assertEqual((rows['boost']['value'], rows['boost']['evidence']),
                         ('not measured', 'CPU setting: needs ~4× the time'))
        self.assertTrue(rows['refresh']['detail'].startswith('Unavailable: no such device'))
        self.assertFalse(rows['refresh']['sensitive'])
        self.assertTrue(panel_data.lever_rows(SETTINGS, panel_data.no_levers(), None, set()))  # Before any votes.

    def test_quarantine_is_read_from_the_runner_and_interactions_must_be_clear(self):
        for found, expected in (({'aspm': {'reason': 'xHCI controller died', 'since': 1.0, 'event': 1.0}},
                                 {'aspm': 'xHCI controller died'}), ({'apst': {}}, {'apst': ''}), (None, {})):
            with mock.patch.object(experiment, 'preferences', lambda: {'quarantine': found}):
                self.assertEqual(panel_data.quarantined(), expected, found)
        # Only lever_effects' multiplicity-corrected `clear`: an interval that excludes 0 alone isn't enough.
        self.assertTrue(panel_data.interaction_clear({'watts_low': 0.1, 'watts_high': 0.4, 'p': 0.001, 'clear': True}))
        self.assertFalse(panel_data.interaction_clear({'watts_low': 0.1, 'watts_high': 0.4, 'p': 0.04, 'clear': False}))
        self.assertFalse(panel_data.interaction_clear({'watts_low': 0.1, 'watts_high': 0.4}))


class HistoryTests(unittest.TestCase):
    def test_a_reload_fetches_only_from_the_newest_minute_and_keeps_events_once(self):
        calls, now = [], [1_000_000.0]
        data = {'minutes': [{'t': 999_000.0}], 'events': [{'event': 'qol', 't1': 999_100.0}]}

        def load(start, end, apps=True):
            calls.append(start)
            return ([m for m in data['minutes'] if start <= m['t'] < end],
                    [e for e in data['events'] if start <= e['t1'] < end])
        history = panel_data.History(load=load, clock=lambda: now[0], ready=lambda: True)
        self.assertEqual(len(history.get()[0]), 1)
        data['minutes'].append({'t': 999_060.0})
        now[0] += 60
        minutes, events = history.get()
        self.assertEqual(calls[-1], 999_000.0)
        self.assertEqual([m['t'] for m in minutes], [999_000.0, 999_060.0])
        self.assertEqual(len(events), 1)


    def test_minutes_loaded_while_the_store_recomputed_are_loaded_again_once_it_is_done(self):
        calls, ready = [], [False]
        data = [{'t': 999_000.0, 'bat': 10.0}, {'t': 999_060.0, 'bat': 11.0}]

        def load(start, end, apps=True):
            calls.append(start)
            return [dict(m) for m in data if start <= m['t'] < end], []
        history = panel_data.History(load=load, clock=lambda: 1_000_000.0, ready=lambda: ready[0])
        history.get()
        version = history.version
        data[0]['bat'] = 99.0  # The collector recomputes an old minute.
        self.assertEqual(history.get()[0][0]['bat'], 10.0)  # Still recomputing: only new minutes are fetched.
        self.assertFalse(history.store_ready)
        ready[0] = True
        minutes, _ = history.get()
        self.assertEqual((minutes[0]['bat'], calls[-1]), (99.0, 1_000_000.0 - panel_data.HISTORY_DAYS * 86400))
        self.assertNotEqual(history.version, version)  # So the history-wide fits are redone.
        history.get()
        self.assertEqual(calls[-1], 999_060.0)  # And then top-ups again.


class CommandTests(unittest.TestCase):
    """What the Experiments tab says after a command, and the app picker."""

    APPLY = ['aspm=saving', 'apst=saving', 'abm=saving', 'refresh=saving']
    REFUSED = ('power-experiment: not applied: aspm (risky: add --risky); '
               'apst (quarantined: `power-experiment release apst`)\n')

    def test_apply_never_calls_a_refused_setting_applied(self):
        note, seconds = panel_data.apply_note(self.APPLY, '', self.REFUSED)
        self.assertEqual(note, 'Applied until reboot: abm, refresh. Not applied: aspm (risky: add --risky); '
                               'apst (quarantined: `power-experiment release apst`).')
        self.assertEqual(seconds, panel_data.NOTE_SECONDS)  # A refusal outlasts the 2 s refresh.
        self.assertEqual(panel_data.apply_note(['abm=saving'], 'On AC: applied settings take effect on battery.\n', ''),
                         ('Applied until reboot (it takes effect on battery): abm.', None))
        note, _ = panel_data.apply_note(['aspm=saving'], '', 'power-experiment: not applied: aspm (risky: add --risky)')
        self.assertEqual(note, 'Nothing applied: aspm (risky: add --risky).')
        self.assertEqual(panel_data.apply_note([], '', '')[0], 'Nothing measured to apply yet.')
        note, _ = panel_data.apply_note(None, 'comfort\n', self.REFUSED)  # goal --apply, combination computed by the CLI.
        self.assertTrue(note.startswith('Applied until reboot. Not applied: aspm (risky: add --risky); apst'))

    def test_failures_and_notices_stay_and_plain_notes_wait_for_the_next_command(self):
        self.assertEqual(panel_data.command_note(False, '', 'watchdog: xHCI controller died; quarantined: aspm.\n'),
                         ('Command failed: watchdog: xHCI controller died; quarantined: aspm.', panel_data.NOTE_SECONDS))
        self.assertEqual(panel_data.command_note(True, '', '', 'Next boot set to PSR on.'), ('Next boot set to PSR on.', None))
        self.assertEqual(panel_data.command_note(True, 'comfort\n', 'Warning: noise\n' + self.REFUSED),
                         ('not applied: aspm (risky: add --risky); apst (quarantined: `power-experiment release apst`).',
                          panel_data.NOTE_SECONDS))
        self.assertEqual(panel_data.command_note(True, 'x', '', lambda out, err: (out * 2, 5)), ('xx', 5))

    def test_the_app_picker_keeps_the_chosen_app_or_a_placeholder(self):
        units = [{'unit': f'app-{i}.scope', 'label': f'App {i}', 'cpu_s': 1000 - i} for i in range(20)]
        names, labels, index = panel_data.picker(units, [], None)
        self.assertEqual((len(names), labels[0], index), (panel_data.PICKER, 'App 0  (16 CPU-min)', 0))
        grown = [dict(u, cpu_s=u['cpu_s'] + 600) for u in reversed(units)]  # CPU times grow, the order changes.
        self.assertIsNone(panel_data.picker(sorted(grown, key=lambda u: -u['cpu_s']), names, 'app-3.scope'))
        # The chosen app fell out of the top: still listed, still chosen.
        names, _, index = panel_data.picker(units[:2] + units[5:] + units[2:5], names[:2] + names[3:], 'app-3.scope')
        self.assertEqual(names[index], 'app-3.scope')
        # The chosen app exited: a placeholder is chosen (Start must not take whatever is first now), and
        # stays chosen as the list changes, until another app is.
        names, labels, index = panel_data.picker(units[1:], names, 'app-0.scope')
        self.assertEqual((names[0], index), (None, 0))
        self.assertIsNone(panel_data.picker(units[1:], names, panel_data.GONE))
        names, _, index = panel_data.picker(units[2:], names, panel_data.GONE)
        self.assertEqual((names[0], index), (None, 0))
        names, _, index = panel_data.picker(units[2:], names, 'app-5.scope')  # Another app chosen: no placeholder.
        self.assertEqual((None in names, names[index]), (False, 'app-5.scope'))

    def test_every_app_scope_reaches_the_picker(self):
        units = [{'unit': f'app-{i}.scope', 'label': f'App {i}', 'cpu_s': 100} for i in range(20)]
        with contextlib.ExitStack() as stack:
            for patch in hermetic():
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(experiment, 'freezable_units', lambda: units))
            result = panel_data.gather(time.time() - 3600, time.time(), history_of([], []))
        self.assertEqual(len(result['units']), 20)


class ExperimentEffectsTests(unittest.TestCase):
    def test_combined_runs_are_left_to_the_setting_model(self):
        minutes, _ = month(days=5)
        effects = report.experiment_effects(minutes, 50, 15)
        self.assertEqual(sorted(e['experiment'] for e in effects), ['aspm', 'freeze-apps'])


if __name__ == '__main__':
    unittest.main()
