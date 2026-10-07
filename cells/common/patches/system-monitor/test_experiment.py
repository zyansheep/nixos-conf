import json
import random
import tempfile
import unittest
from pathlib import Path
import itertools

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

    def test_unit_labels(self):
        self.assertEqual(unit_label('app-niri-floorp-2329.scope'), 'floorp')
        self.assertEqual(unit_label('app-niri-signal\\x2ddesktop-2342.scope'), 'signal-desktop')


if __name__ == '__main__':
    unittest.main()
