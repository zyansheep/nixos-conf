import tempfile
import unittest
from pathlib import Path

import throttle
from throttle import UNLIMITED, Throttle


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apps, self.proc, self.on_battery = root / 'app.slice', root / 'proc', True
        for scope, pid in (('app-niri-floorp-2240.scope', 2240), ('app-md.Obsidian-2243.scope', 2243),
                           ('app-com.t3tools.T3Code-272490.scope', 272490), ('app-niri-fcitx5-2259.scope', 2259),
                           ('app-niri-foot-9.scope', 9)):
            (self.apps / scope).mkdir(parents=True)
            (self.apps / scope / 'cpu.max').write_text(UNLIMITED + '\n')
            (self.proc / str(pid)).mkdir(parents=True)
            (self.proc / str(pid) / 'cgroup').write_text(
                f'0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n')
        (self.apps / 'waybar.service').mkdir()  # Services are not apps.
        self.throttle = Throttle(self.apps, quota=5, battery=lambda: self.on_battery, proc=self.proc)
        self.throttle.window_event({'WindowsChanged': {'windows': [
            {'id': 1, 'pid': 2240, 'is_focused': False}, {'id': 2, 'pid': 2243, 'is_focused': True},
            {'id': 3, 'pid': 9, 'is_focused': False}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def limits(self):
        return {p.parent.name: p.read_text().strip() for p in self.apps.glob('*/cpu.max')}

    def test_everything_but_the_focused_app_and_the_kept_ones_is_capped(self):
        self.assertEqual(throttle.label(self.throttle.apply()[0]), 'floorp')
        limits = self.limits()
        self.assertEqual(limits['app-niri-floorp-2240.scope'], '5000 100000')
        self.assertEqual(limits['app-niri-foot-9.scope'], '5000 100000')
        for free in ('app-md.Obsidian-2243.scope', 'app-com.t3tools.T3Code-272490.scope', 'app-niri-fcitx5-2259.scope'):
            self.assertEqual(limits[free], UNLIMITED)
        # Focus moves to the terminal: it is free at once, Obsidian is capped.
        self.throttle.window_event({'WindowFocusChanged': {'id': 3}})
        self.throttle.apply()
        limits = self.limits()
        self.assertEqual((limits['app-niri-foot-9.scope'], limits['app-md.Obsidian-2243.scope']), (UNLIMITED, '5000 100000'))

    def test_apps_playing_media_are_left_alone_and_so_is_everything_on_ac(self):
        node = {'id': 80, 'type': 'PipeWire:Interface:Node',
                'info': {'state': 'idle', 'props': {'media.class': 'Stream/Output/Audio', 'application.process.id': 2240}}}
        self.throttle.media_event([node])
        self.assertIn('app-niri-floorp-2240.scope', self.throttle.apply())  # An open, idle stream is not playing.
        self.throttle.media_event([{'id': 80, 'type': 'PipeWire:Interface:Node', 'info': {'state': 'running'}}])
        self.assertNotIn('app-niri-floorp-2240.scope', self.throttle.apply())  # A partial update: now playing.
        self.throttle.media_event([{'id': 80, 'type': 'PipeWire:Interface:Node', 'info': None}])
        self.assertIn('app-niri-floorp-2240.scope', self.throttle.apply())  # The stream is gone.
        self.on_battery = False
        self.assertEqual(self.throttle.apply(), [])
        self.assertEqual(set(self.limits().values()), {UNLIMITED})

    def test_release_lifts_every_quota_even_one_left_by_a_crash(self):
        (self.apps / 'app-niri-vesktop-7.scope').mkdir()
        (self.apps / 'app-niri-vesktop-7.scope' / 'cpu.max').write_text('5000 100000\n')
        self.throttle.apply()
        self.throttle.release()
        self.assertEqual(set(self.limits().values()), {UNLIMITED})

    def test_labels(self):
        self.assertEqual([throttle.label(s) for s in ('app-niri-floorp-2240.scope', 'app-md.Obsidian-2243.scope',
                                                       'app-niri-signal\\x2ddesktop-2247.scope')],
                         ['floorp', 'Obsidian', 'signal-desktop'])

    def test_pw_dump_batches_and_niri_lines_parse_across_reads(self):
        stream = type('Stream', (), {})()
        stream.buffer = '[{"id": 1}]\n[{"id": 2},'
        self.assertEqual(throttle.batches(stream), [[{'id': 1}]])
        stream.buffer += ' {"id": 3}]\n'
        self.assertEqual(throttle.batches(stream), [[{'id': 2}, {'id': 3}]])
        stream.buffer = '{"WindowFocusChanged": {"id": 4}}\n{"Window'
        self.assertEqual(throttle.lines(stream), [{'WindowFocusChanged': {'id': 4}}])
        self.assertEqual(stream.buffer, '{"Window')


if __name__ == '__main__':
    unittest.main()
