import json
import random
import tempfile
import unittest
from pathlib import Path
from experiment import Setting, experiment, schedule, unit_label


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
    def __init__(self, value='performance', ac_managed=False):
        self.value, self.history = value, []

        def set_value(v):
            self.history.append(v)
            self.value = v
        super().__init__('aspm', 'ASPM', '', lambda: self.value, set_value,
                         lambda cur: 'powersupersave', ac_managed=ac_managed)


class ExperimentTests(unittest.TestCase):
    def run_experiment(self, spec, minutes, battery=lambda: True, clock=None, **kwargs):
        clock = clock or FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            experiment(spec, minutes, clock=clock, battery=battery, locked=lambda: True,
                       state_path=path, rng=random.Random(1), **kwargs)
            return json.loads(path.read_text()), clock

    def test_schedule_is_balanced_pairs(self):
        arms = schedule(random.Random(3))
        pairs = [sorted([next(arms), next(arms)]) for _ in range(20)]
        self.assertTrue(all(pair == ['A', 'B'] for pair in pairs))

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
        self.assertEqual(state['blocks'], 2)  # 8 min on battery out of 18.
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

    def test_unit_labels(self):
        self.assertEqual(unit_label('app-niri-floorp-2329.scope'), 'floorp')
        self.assertEqual(unit_label('app-niri-signal\\x2ddesktop-2342.scope'), 'signal-desktop')


if __name__ == '__main__':
    unittest.main()
