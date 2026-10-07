import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import qol


class QolTests(unittest.TestCase):
    def test_a_thumbs_down_rests_experiments_until_it_runs_out_or_is_resumed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(qol.subprocess, 'run') as signal:
            path = Path(tmp) / 'qol.json'
            self.assertEqual((qol.votes(path), qol.resting(path, now=100)), ([], 0.0))
            qol.vote('down', path, now=100)
            self.assertIn('-RTMIN+12', signal.call_args.args[0])
            self.assertEqual(qol.resting(path, now=160), qol.COOLDOWN - 60)
            self.assertEqual(qol.resting(path, now=100 + qol.COOLDOWN + 1), 0)
            qol.vote('up', path, now=200)  # 👍 is about something especially good, not "resume".
            self.assertEqual(qol.resting(path, now=210), qol.COOLDOWN - 110)
            qol.resume(path, now=220)
            self.assertEqual(qol.resting(path, now=230), 0)
            self.assertEqual(qol.votes(path), [(100.0, 'down'), (200.0, 'up')])
            with self.assertRaises(ValueError):
                qol.vote('sideways', path)

    def test_old_votes_roll_off(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(qol.subprocess, 'run'):
            path = Path(tmp) / 'qol.json'
            for t in range(qol.KEEP + 5):
                qol.vote('up', path, now=float(t + 1))
            self.assertEqual(len(qol.votes(path)), qol.KEEP)
            self.assertEqual(qol.votes(path)[0][0], 6.0)

    def test_waybar_buttons(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(qol.subprocess, 'run'):
            path = Path(tmp) / 'qol.json'
            qol.vote('down', path, now=1000)
            down, up = qol.waybar('down', path, now=1005), qol.waybar('up', path, now=1005)
            self.assertEqual((down['text'], up['text']), ('', ''))
            self.assertEqual(down['class'], ['down', 'recent', 'resting'])
            self.assertEqual(up['class'], ['up'])
            self.assertIn('Resting', down['tooltip'])
            json.dumps(down)


if __name__ == '__main__':
    unittest.main()
