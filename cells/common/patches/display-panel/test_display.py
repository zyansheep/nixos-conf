import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("display_panel", Path(__file__).with_name("display-panel.py"))
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class StateTest(unittest.TestCase):
    def test_round_trip_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub/state.json"
            self.assertEqual(panel.load_state(path), panel.DEFAULTS)
            panel.save_state({"night": True, "intensity": 80, "grayscale": True}, path)
            self.assertEqual(panel.load_state(path), {"night": True, "intensity": 80, "grayscale": True})

    def test_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"night": "yes", "intensity": 900, "extra": 1}')
            self.assertEqual(panel.load_state(path), {**panel.DEFAULTS, "intensity": 100})
            path.write_text("not json")
            self.assertEqual(panel.load_state(path), panel.DEFAULTS)

    def test_temperature(self):
        self.assertEqual(panel.temperature({"night": False, "intensity": 100}), 6500)
        self.assertEqual(panel.temperature({"night": True, "intensity": 100}), 2500)
        self.assertEqual(panel.temperature({"night": True, "intensity": 50}), 4500)

    def test_brightness_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp)
            (device / "brightness").write_text("16384\n")
            (device / "max_brightness").write_text("65535\n")
            self.assertEqual(panel.brightness_percent(device), 25)
            (device / "max_brightness").write_text("0\n")
            self.assertIsNone(panel.brightness_percent(device))
            self.assertIsNone(panel.brightness_percent(device / "missing"))


if __name__ == "__main__":
    unittest.main()
