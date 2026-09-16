import importlib.util
import subprocess
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace
from pathlib import Path

spec = importlib.util.spec_from_file_location("audio", Path(__file__).with_name("audio-sidebar.py"))
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


class AudioTests(unittest.TestCase):
    def test_channels_and_missing_volume(self):
        self.assertEqual(audio.volume({"volume": {"left": {"value": 32768}, "right": {"value": 65536}}}), 75)
        self.assertEqual(audio.volume({}), 0)

    def test_pipewire_monitor_sources(self):
        self.assertTrue(audio.is_monitor({"properties": {"device.class": "monitor"}}))
        self.assertTrue(audio.is_monitor({"monitor_of_sink": 0}))
        self.assertFalse(audio.is_monitor({"name": "microphone", "monitor_of_sink": 4294967295}))

    def test_names_are_plain_text(self):
        self.assertEqual(audio.description({"properties": {"application.name": "<App & name>"}}), "<App & name>")

    def test_commands_do_not_use_a_shell(self):
        with patch.object(audio.subprocess, "run") as run:
            run.return_value.stdout = ""
            audio.pactl("set-default-sink", "device with spaces; $(false)")
            self.assertEqual(run.call_args.args[0], ["pactl", "set-default-sink", "device with spaces; $(false)"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertTrue(run.call_args.kwargs["check"])

    def test_server_failure_is_not_an_empty_success(self):
        with patch.object(audio.subprocess, "run", side_effect=subprocess.TimeoutExpired("pactl", 4)):
            with self.assertRaises(subprocess.TimeoutExpired):
                audio.snapshot()

    def test_volume_does_not_rebuild_widgets_but_hotplug_does(self):
        state = {k: [] for k in ("sinks", "sources", "sink-inputs", "source-outputs")}
        device = {"index": 1, "name": "speaker", "volume": {}}
        state["sinks"] = [device]
        original = audio.topology(state)
        device["mute"] = True
        device["volume"] = {"mono": {"value": 50000}}
        self.assertEqual(original, audio.topology(state))
        state["sinks"] = []
        self.assertNotEqual(original, audio.topology(state))


class ResidentTests(unittest.TestCase):
    def test_hidden_window_does_not_poll_but_startup_can_preload(self):
        popup = SimpleNamespace(window=Mock(), pending=0, busy=lambda: False,
                                pool=Mock(), load=Mock())
        popup.window.get_visible.return_value = False
        audio.AudioPopup.refresh(popup)
        popup.pool.submit.assert_not_called()
        audio.AudioPopup.refresh(popup, force=True)
        popup.pool.submit.assert_called_once_with(popup.load)
        self.assertEqual(popup.pending, 1)

    def test_toggle_reuses_window_and_close_hides_without_destroying(self):
        popup = SimpleNamespace(window=Mock(), dragging=True, refresh=Mock())
        popup.close = lambda: audio.AudioPopup.close(popup)
        window = popup.window
        window.get_visible.return_value = True
        audio.AudioPopup.toggle(popup)
        window.set_visible.assert_called_once_with(False)
        window.destroy.assert_not_called()
        self.assertFalse(popup.dragging)
        window.get_visible.return_value = False
        audio.AudioPopup.toggle(popup)
        window.present.assert_called_once()
        self.assertIs(popup.window, window)
        popup.refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
