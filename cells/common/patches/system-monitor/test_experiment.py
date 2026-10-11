import json
import random
import tempfile
import unittest
from pathlib import Path
import itertools
from unittest import mock

import experiment as experiment_module
from experiment import Setting, design, experiment, unit_label


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.hooks = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for hook in self.hooks:
            hook(self.now)

    def suspended_since(self, mark):
        return False, (self.now, self.now)


class FakeSetting(Setting):
    def __init__(self, value='performance', ac_managed=False, name='aspm', alternative='powersupersave'):
        self.value, self.history = value, []

        def set_value(v):
            self.history.append(v)
            self.value = v
        super().__init__(name, name.upper(), '', lambda: self.value, set_value,
                         lambda cur: alternative, ac_managed=ac_managed)


class ExperimentTests(unittest.TestCase):
    def run_experiment(self, spec, minutes, battery=lambda: True, clock=None, **kwargs):
        clock = clock or FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            experiment(spec, minutes, clock=clock, battery=battery, locked=lambda: True,
                       state_path=path, rng=random.Random(1), **kwargs)
            return json.loads(path.read_text()), clock

    def test_one_setting_is_shuffled_ab_pairs(self):
        blocks = design(['aspm'], random.Random(3))
        pairs = [sorted([next(blocks)['aspm'], next(blocks)['aspm']]) for _ in range(20)]
        self.assertTrue(all(pair == ['A', 'B'] for pair in pairs))

    def test_up_to_four_settings_run_every_combination_each_cycle(self):
        blocks = design(['a', 'b', 'c'], random.Random(4))
        for _ in range(3):
            cycle = {tuple(block[n] for n in 'abc') for block in (next(blocks) for _ in range(8))}
            self.assertEqual(cycle, set(itertools.product('AB', repeat=3)))

    def test_more_settings_stay_balanced_per_cycle(self):
        names = ['a', 'b', 'c', 'd', 'e', 'f']
        blocks = design(names, random.Random(5))
        cycle = [next(blocks) for _ in range(16)]
        for name in names:
            self.assertEqual(sum(block[name] == 'B' for block in cycle), 8)
        self.assertEqual(len({tuple(block[n] for n in 'abcd') for block in cycle}), 16)

    def test_blocks_alternate_and_the_original_is_restored(self):
        spec = FakeSetting()
        state, _ = self.run_experiment(spec, 16)  # Four 240 s blocks.
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['blocks'], 4)
        self.assertEqual(spec.value, 'performance')
        self.assertEqual(spec.history.count('powersupersave'), 2)

    def test_on_ac_pauses_and_restores_unless_udev_manages_the_setting(self):
        clock = FakeClock()
        plugged = {'until': clock.now + 600}
        battery = lambda: clock.now >= plugged['until']
        spec = FakeSetting()
        state, _ = self.run_experiment(spec, 18, battery=battery, clock=clock)
        self.assertEqual(state['blocks'], 5)  # 18 min of battery time after 10 on AC: 4 + 4 + 4 + 4 + 2.
        self.assertAlmostEqual(state['collected'], 18 * 60)
        managed = FakeSetting(value='power-saver', ac_managed=True)
        managed.value = 'balanced'  # udev's AC value; the runner must leave it alone.
        clock = FakeClock()
        with mock.patch.object(experiment_module, 'GIVE_UP', 3600):  # Always on AC: give up after an hour, not a week.
            self.run_experiment(managed, 5, battery=lambda: False, clock=clock)
        self.assertEqual(managed.history, [])

    def test_external_restore_ends_the_block_and_the_arm_is_reapplied(self):
        clock, spec = FakeClock(), FakeSetting()
        # The pre-sleep hook restores ASPM while a B block is running.
        clock.hooks.append(lambda now: setattr(spec, 'value', 'performance') if 1100 <= now < 1105 else None)
        state, _ = self.run_experiment(spec, 8, clock=clock)
        self.assertGreaterEqual(state['blocks'], 3)
        self.assertEqual(spec.value, 'performance')

    def test_settings_randomized_together_are_tagged_and_all_restored(self):
        clock = FakeClock()
        aspm, apst = FakeSetting(name='aspm'), FakeSetting(value='0', name='apst', alternative='100000')
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'

            def watch(_now):
                state = json.loads(path.read_text()) if path.exists() else {}
                if state.get('status') == 'running':
                    seen.append((state['arm'], state['value'], {'aspm': aspm.value, 'apst': apst.value}))
            clock.hooks.append(watch)
            experiment([aspm, apst], 32, clock=clock, battery=lambda: True, locked=lambda: True,
                       state_path=path, rng=random.Random(2))
            final = json.loads(path.read_text())
        self.assertEqual((final['name'], final['blocks']), ('aspm+apst', 8))
        self.assertEqual((aspm.value, apst.value), ('performance', '0'))
        self.assertEqual(sorted({arm for arm, _, _ in seen}), ['AA', 'AB', 'BA', 'BB'])
        self.assertTrue(all(value == actual for _, value, actual in seen))

    def test_automatic_blocks_follow_the_plan_rest_after_a_thumbs_down_and_restore(self):
        clock = FakeClock()
        aspm = FakeSetting(name='aspm')
        aspm.levels = lambda: ('powersupersave', 'performance')
        apst = FakeSetting(value='0', name='apst', alternative='100000')
        apst.levels = lambda: ('100000', '0')
        specs = {'aspm': aspm, 'apst': apst}
        # A 👎 5 minutes in (mid second block) rests experiments until 12; they are disabled at 30.
        rest = lambda: max(0.0, 1720 - clock.now) if clock.now >= 1300 else 0.0
        plans = []

        def choose(names, present, run):
            plans.append(present)
            return {name: len(plans) % 2 == 1 for name in names}, {}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            states = []
            clock.hooks.append(lambda now: states.append(json.loads(path.read_text())) if path.exists() else None)
            from experiment import automatic
            automatic(specs, clock=clock, battery=lambda: True, locked=lambda: False,
                      resting=rest, enabled=lambda: clock.now < 1000 + 30 * 60,
                      names=lambda _specs: ['aspm', 'apst'], choose=choose, state_path=path)
            final = json.loads(path.read_text())
        self.assertEqual(final['status'], 'finished')
        self.assertEqual((aspm.value, apst.value), ('performance', '0'))  # Restored.
        self.assertIn('resting after your 👎', {s.get('reason') for s in states})
        running = [s for s in states if s['status'] == 'running']
        self.assertEqual(running[0]['value'], {'aspm': 'powersupersave', 'apst': '100000'})
        self.assertTrue(all(plans))
        self.assertGreaterEqual(final['blocks'], 5)

    def test_automatic_pauses_instead_of_crashing_when_a_setting_fails(self):
        clock = FakeClock()
        aspm = FakeSetting(name='aspm')
        aspm.levels = lambda: ('powersupersave', 'performance')
        wifi = FakeSetting(value='off', name='wifi-ps', alternative='on')
        wifi.levels = lambda: ('on', 'off')
        present = wifi.get

        def gone():  # The Wi-Fi interface disappears for five minutes.
            if 1600 <= clock.now < 1900:
                raise RuntimeError('iw: No such device (-19)')
            return present()
        wifi.get = gone
        failures, works = [RuntimeError('powerprofilesctl: Failed to activate CPU driver')], aspm.set

        def flaky(value):
            if failures:
                raise failures.pop()
            works(value)
        aspm.set = flaky
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            states = []
            clock.hooks.append(lambda now: states.append(json.loads(path.read_text())) if path.exists() else None)
            from experiment import automatic
            automatic({'aspm': aspm, 'wifi-ps': wifi}, clock=clock, battery=lambda: True, locked=lambda: True,
                      resting=lambda: 0.0, enabled=lambda: clock.now < 1000 + 30 * 60,
                      names=lambda _specs: ['aspm', 'wifi-ps'],
                      choose=lambda names, present, run: ({n: True for n in names}, {}), state_path=path)
            final = json.loads(path.read_text())
        reasons = {s.get('reason') for s in states}
        self.assertIn('could not set ASPM', reasons)
        self.assertIn('WIFI-PS unavailable', reasons)
        self.assertEqual(final['status'], 'finished')
        self.assertEqual((aspm.value, wifi.value), ('performance', 'off'))  # Restored.
        self.assertGreater(sum(s['status'] == 'running' for s in states), 0)

    def test_a_goal_applies_its_whole_best_combination_unless_experiments_own_the_settings(self):
        best = {'battery': {'apply': ['aspm=saving', 'apst=normal']}, 'comfort': {'apply': []}}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'experiment.json'
            with mock.patch.object(experiment_module, 'STATE', state), \
                    mock.patch.object(experiment_module, 'command_apply') as apply:
                experiment_module.apply_goal('battery', recommend=lambda now: best)
                apply.assert_called_once_with(['aspm=saving', 'apst=normal'])
                experiment_module.apply_goal('comfort', recommend=lambda now: best)  # Nothing measured.
                state.write_text(json.dumps({'mode': 'auto', 'status': 'paused'}))
                experiment_module.apply_goal('battery', recommend=lambda now: best)
                self.assertEqual(apply.call_count, 1)

    def test_a_stale_recommendation_is_computed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'recommend.json'
            path.write_text(json.dumps({'updated': 1000}))
            with mock.patch.object(experiment_module, 'RECOMMEND', path), \
                    mock.patch('eta.write_recommendations', return_value={'fresh': True}), \
                    mock.patch('report.load_range', return_value=([], [])):
                self.assertEqual(experiment_module.recommendation(now=1100), {'updated': 1000})
                self.assertEqual(experiment_module.recommendation(now=1000 + 3601), {'fresh': True})

    def test_the_cpu_cap_reads_none_at_the_cpus_own_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / 'policy0'
            policy.mkdir()
            (policy / 'cpuinfo_max_freq').write_text('3501000\n')
            with mock.patch.object(experiment_module, 'CPUFREQ', Path(tmp)):
                (policy / 'scaling_max_freq').write_text('3501000\n')
                self.assertEqual(experiment_module.cpu_cap_get(), 'none')
                (policy / 'scaling_max_freq').write_text('2000000\n')
                self.assertEqual(experiment_module.cpu_cap_get(), experiment_module.CPU_CAP)
        specs = experiment_module.catalog()
        self.assertEqual((specs['cpu-cap'].levels(), specs['gpu-dpm'].levels()), (('2000', 'none'), ('low', 'auto')))

    def test_boost_is_on_only_globally_and_per_cpu_and_profile_changes_keep_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpufreq, calls = Path(tmp), []
            write = lambda name, value: (cpufreq / name).write_text(f'{value}\n')
            write('boost', 1)
            for i in range(2):
                (cpufreq / f'policy{i}').mkdir()
                write(f'policy{i}/boost', 1)

            def run(command, check=True):
                calls.append(command)
                if command[:2] == ['powerprofilesctl', 'set']:  # The daemon: per-CPU boost off for power-saver.
                    if (cpufreq / 'boost').read_text().strip() == '0':
                        raise RuntimeError('Failed to activate CPU driver: Invalid argument')
                    for i in range(2):
                        write(f'policy{i}/boost', 0 if command[2] == 'power-saver' else 1)
                elif command[-2] == 'boost':  # power-lab: global on, per CPU as asked.
                    write('boost', 1)
                    for i in range(2):
                        write(f'policy{i}/boost', command[-1])
                return ''
            lab = experiment_module.POWER_LAB
            with mock.patch.object(experiment_module, 'CPUFREQ', cpufreq), mock.patch.object(experiment_module, 'run', run):
                self.assertEqual(experiment_module.boost_get(), '1')
                experiment_module.profile_set('power-saver')
                self.assertEqual(experiment_module.boost_get(), '1')  # Put back after the daemon turned it off.
                self.assertEqual(calls[-1], lab + ['boost', '1'])
                write('policy1/boost', 0)
                self.assertEqual(experiment_module.boost_get(), '0')
                write('boost', 0)  # Off globally (the old power-lab): the daemon refuses to switch.
                experiment_module.profile_set('balanced')
                self.assertEqual(calls[-3:], [lab + ['boost', '0'], ['powerprofilesctl', 'set', 'balanced'],
                                              lab + ['boost', '0']])
                self.assertEqual((experiment_module.boost_get(), (cpufreq / 'boost').read_text().strip()), ('0', '1'))

    def test_unit_labels(self):
        self.assertEqual(unit_label('app-niri-floorp-2329.scope'), 'floorp')
        self.assertEqual(unit_label('app-niri-signal\\x2ddesktop-2342.scope'), 'signal-desktop')


if __name__ == '__main__':
    unittest.main()
