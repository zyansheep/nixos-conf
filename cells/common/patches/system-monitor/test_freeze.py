import json
import tempfile
import unittest
from pathlib import Path

import freeze
from freeze import AFTER, EVERY, THAW, Freezer


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apps, self.proc, self.on_battery = root / 'app.slice', root / 'proc', True
        # Obsidian's two scopes share its launch pid, as every Electron app's here do.
        for scope, pid in (('app-md.Obsidian-2243.scope', 2243), ('app-niri-obsidian-2243.scope', 2250),
                           ('app-niri-floorp-2240.scope', 2240), ('app-niri-steam-3000.scope', 3000)):
            self.scope(scope, pid)
        self.now = 10_000.0
        self.freezer = Freezer(['obsidian', 'steam'], self.apps, battery=lambda: self.on_battery, proc=self.proc,
                               clock=lambda: self.now)
        self.freezer.window_event({'WorkspacesChanged': {'workspaces': [
            {'id': 1, 'output': 'eDP-1', 'is_active': True}, {'id': 2, 'output': 'eDP-1', 'is_active': False}]}})
        self.freezer.window_event({'WindowsChanged': {'windows': [
            {'id': 1, 'pid': 2240, 'workspace_id': 1, 'is_focused': True},
            {'id': 2, 'pid': 2243, 'workspace_id': 2, 'is_focused': False},
            {'id': 3, 'pid': 3000, 'workspace_id': 2, 'is_focused': False}]}})

    def tearDown(self):
        self.tmp.cleanup()

    def scope(self, scope, pid):
        (self.apps / scope).mkdir(parents=True)
        (self.apps / scope / 'cgroup.freeze').write_text('0\n')
        (self.apps / scope / 'cpu.stat').write_text('usage_usec 0\n')
        (self.proc / str(pid)).mkdir(parents=True)
        (self.proc / str(pid) / 'cgroup').write_text(f'0::/user.slice/user-1000.slice/user@1000.service/app.slice/{scope}\n')

    def frozen(self):
        return {p.parent.name for p in self.apps.glob('*/cgroup.freeze') if p.read_text().strip() == '1'}

    def wait(self, seconds, step=5):
        """Advance the clock, stepping the freezer as run() does every RESCAN."""
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now + step)
            self.freezer.step(True)

    OBSIDIAN = {'app-md.Obsidian-2243.scope', 'app-niri-obsidian-2243.scope'}

    def test_a_chosen_app_unused_for_a_while_freezes_and_focus_thaws_it(self):
        self.freezer.step(True)
        self.assertEqual(self.frozen(), set())  # Just seen: not before AFTER.
        self.wait(AFTER)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})  # Both scopes; not Floorp
        state = self.freezer.step(True)
        self.assertEqual((state['frozen'], state['watching']), (['obsidian', 'steam'], ['obsidian', 'steam']))
        self.freezer.window_event({'WindowFocusChanged': {'id': 2}})
        self.freezer.step(True)
        self.assertEqual(self.frozen(), {'app-niri-steam-3000.scope'})  # At once
        self.freezer.window_event({'WindowFocusChanged': {'id': 1}})
        self.wait(AFTER - 10)
        self.assertNotIn('app-md.Obsidian-2243.scope', self.frozen())  # Its clock started again

    def test_apps_on_a_workspace_in_view_stay_running(self):
        self.freezer.window_event({'WorkspaceActivated': {'id': 2, 'focused': True}})
        self.wait(AFTER + 60)
        self.assertEqual(self.frozen(), set())
        self.freezer.window_event({'WorkspaceActivated': {'id': 1, 'focused': True}})  # Switched away
        self.wait(AFTER - 10)
        self.assertEqual(self.frozen(), set())
        self.wait(20)
        self.assertEqual(self.frozen(), self.OBSIDIAN | {'app-niri-steam-3000.scope'})

    def test_niris_focus_times_carry_over_a_restart(self):
        self.freezer.window_event({'WindowOpenedOrChanged': {'window': {
            'id': 2, 'pid': 2243, 'workspace_id': 2, 'is_focused': False,
            'focus_timestamp': {'secs': int(self.now - AFTER - 1), 'nanos': 0}}}})
        self.freezer.step(True)
        self.assertEqual(self.frozen(), self.OBSIDIAN)  # Unfocused for AFTER already

    def test_frozen_apps_thaw_now_and_then_to_catch_up(self):
        self.freezer.step(True)
        self.wait(AFTER)
        self.wait(EVERY - 5)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.wait(5)
        self.assertFalse(self.OBSIDIAN & self.frozen())  # A thaw
        self.wait(THAW)
        self.assertTrue(self.OBSIDIAN <= self.frozen())  # Over

    def test_busy_apps_media_and_ac_keep_apps_running(self):
        stat = self.apps / 'app-niri-steam-3000.scope' / 'cpu.stat'
        for _ in range((AFTER + 120) // 5):  # Half a core: a download unpacking, say.
            self.now += 5
            stat.write_text(f'usage_usec {int((self.now - 10_000) * 0.5e6)}\n')
            self.freezer.step(True)
        self.assertNotIn('app-niri-steam-3000.scope', self.frozen())
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.freezer.media_event([{'id': 7, 'type': 'PipeWire:Interface:Node', 'info': {
            'state': 'running', 'props': {'media.class': 'Stream/Output/Audio', 'application.process.id': 2250}}}])
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())  # Playing: in use
        self.freezer.media_event([{'id': 7, 'info': None}])
        self.wait(AFTER)
        self.assertTrue(self.OBSIDIAN <= self.frozen())
        self.on_battery = False
        self.freezer.step(True)
        self.assertEqual(self.frozen(), set())
        self.on_battery = True
        self.freezer.step(False)  # PipeWire down: nothing stays frozen
        self.assertEqual(self.frozen(), set())

    def test_starting_a_frozen_app_again_thaws_it(self):
        self.wait(AFTER)
        self.scope('app-niri-obsidian-4000.scope', 4000)  # `obsidian obsidian://open?…` from another app
        self.freezer.step(True)
        self.assertFalse(self.OBSIDIAN & self.frozen())

    def test_only_its_own_freezes_are_undone(self):
        (self.apps / 'app-niri-floorp-2240.scope' / 'cgroup.freeze').write_text('1\n')  # A power-experiment block
        self.wait(AFTER)
        self.freezer.release()
        self.assertEqual(self.frozen(), {'app-niri-floorp-2240.scope'})
        self.wait(5)  # Frozen again; then a crash leaves them so, and the next start thaws them.
        state = self.apps.parent / 'freeze.json'
        state.write_text(json.dumps(self.freezer.step(True)))
        freeze.release_left(state, self.apps)
        self.assertEqual(self.frozen(), {'app-niri-floorp-2240.scope'})
        self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
