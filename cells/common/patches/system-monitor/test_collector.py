import json
import tempfile
import unittest
from pathlib import Path
from collector import History, processes, temperatures, process_label, process_identity, browser_labels, TICKS, PAGE


class MonitorTests(unittest.TestCase):
    def test_browser_content_is_separated_and_helpers_are_readable(self):
        self.assertEqual(process_label(123, 'Isolated Web Co', 'Floorp', ''), 'Floorp web · PID 123')
        self.assertNotEqual(process_label(123, 'Isolated Web Co', 'Floorp', ''),
                            process_label(124, 'Isolated Web Co', 'Floorp', ''))
        self.assertEqual(process_label(123, 'RDD Process', 'Floorp', ''), 'Floorp · media decoder')
        self.assertEqual(process_label(123, 'WebExtensions', 'Floorp', ''), 'Floorp · extensions')
        self.assertEqual(process_label(123, '.waybar-wrapped', None, ''), 'Waybar')

    def test_electron_entry_and_role_without_using_document_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'exe').symlink_to('/nix/store/example-electron/bin/electron')
            (p/'cmdline').write_bytes(b'electron\0/nix/store/example/lib/vesktop/app.asar\0--type=renderer\0')
            self.assertEqual(process_identity(p, 'electron'), ('Vesktop', 'renderer'))
            (p/'cmdline').write_bytes(b'electron\0https://example.com/vesktop/app.asar\0')
            self.assertEqual(process_identity(p, 'electron'), ('Electron', ''))

    def test_browser_ancestry_when_executable_links_are_inaccessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for pid, parent, name in [(42, 1, '.floorp-wrapped'), (43, 42, 'Isolated Web Co')]:
                directory=root/str(pid); directory.mkdir()
                fields=['0']*22; fields[0]='S'; fields[1]=str(parent)
                fields[19]='100'; fields[21]='4'
                (directory/'stat').write_text(f'{pid} ({name}) '+' '.join(fields))
            self.assertEqual(processes(root)[(43,100)][0], 'Floorp web · PID 43')

    def test_rolling_cpu_and_current_memory_have_different_order(self):
        h = History(cores=2)
        a = {(1, 0): ('busy', 0, 10), (2, 0): ('large', 0, 2**30)}
        h.sample(0, a)
        for t in range(2, 62, 2):
            state = h.sample(t, {(1, 0): ('busy', t, 10), (2, 0): ('large', 0, 2**30)})
        self.assertEqual(state['coverage'], 60)
        self.assertEqual(state['cpu'][0], {'name': 'busy', 'value': '50.0%'})
        self.assertEqual(state['memory'][0]['name'], 'large')
        for t in range(62, 122, 2):
            state = h.sample(t, {(1, 0): ('busy', 60, 10)})
        self.assertEqual(state['cpu'], [])

    def test_pid_reuse_is_not_old_process_cpu_and_suspend_resets(self):
        h = History(cores=1)
        h.sample(100, {(1, 0): ('old', 500, 10)})
        state = h.sample(102, {(1, 101*TICKS): ('new', 1, 20)})
        self.assertEqual(state['cpu'], [{'name': 'new', 'value': '50.0%'}])
        state = h.sample(200, {(1, 101*TICKS): ('new', 2, 20)})
        self.assertEqual(state['coverage'], 0)
        self.assertEqual(state['cpu'], [])

    def test_exited_program_stays_in_window_and_names_aggregate(self):
        h = History(cores=1)
        h.sample(0, {(1, 0): ('app', 0, 10), (2, 0): ('app', 0, 20)})
        state = h.sample(2, {(1, 0): ('app', .5, 10), (2, 0): ('app', .5, 20)})
        self.assertEqual(len(state['cpu']), 1)
        self.assertEqual(len(state['memory']), 1)
        state = h.sample(4, {})
        self.assertEqual(state['cpu'], [{'name': 'app', 'value': '25.0%'}])
        self.assertEqual(state['memory'], [])

    def test_browser_bridge_freshness_pid_reuse_and_display_only_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            current = {(42, 100): ('Floorp web · PID 42', 1, 100)}
            h = History(cores=1)
            h.sample(0, dict(current))
            current[(42, 100)] = ('Floorp web · PID 42', 2, 100)
            snapshot = {'timestamp': 10, 'processes': [
                {'pid': 42, 'start': 100, 'origin': 'https://example.org', 'titles': ['Example']}]}
            path = directory / '1.json'
            path.write_text(json.dumps(snapshot))
            state = h.sample(2, current)
            browser_labels(state, current, directory, now=10)
            self.assertIn('example.org · Example', state['cpu'][0]['name'])
            self.assertEqual(state['cpu'][0]['value'], '50.0%')
            self.assertEqual(h.previous[(42, 100)][0], 'Floorp web · PID 42')
            for now, start in [(30, 100), (10, 101)]:
                snapshot['processes'][0]['start'] = start
                path.write_text(json.dumps(snapshot))
                state = {'cpu': [{'name': 'Floorp web · PID 42'}], 'memory': []}
                browser_labels(state, current, directory, now=now)
                self.assertEqual(state['cpu'][0]['name'], 'Floorp web · PID 42')
            path.write_text('{broken')
            browser_labels(state, current, directory, now=10)

    def test_rankings_cap_at_100_and_keep_descending_order(self):
        h = History(cores=1)
        h.sample(0, {(i, 0): (f'app-{i}', 0, i * 2**20) for i in range(1, 151)})
        state = h.sample(2, {(i, 0): (f'app-{i}', i / 1000, i * 2**20)
                             for i in range(1, 151)})
        for kind in ('cpu', 'memory'):
            self.assertEqual(len(state[kind]), 100)
            self.assertEqual([row['name'] for row in state[kind]],
                             [f'app-{i}' for i in range(150, 50, -1)])

    def test_proc_comm_parentheses_and_unreadable_sensors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'12').mkdir()
            fields = ['0']*22
            fields[0] = 'S'; fields[11] = str(TICKS); fields[12] = str(TICKS)
            fields[19] = '123'; fields[21] = '4'
            (root/'12'/'stat').write_text('12 (name ) with spaces) '+' '.join(fields))
            self.assertEqual(processes(root)[(12,123)], ('name ) with spaces',2,4*PAGE))
            chip=root/'hwmon0';chip.mkdir();(chip/'name').write_text('cpu')
            (chip/'temp1_input').write_text('42000');(chip/'temp1_label').write_text('Package')
            (chip/'temp2_input').write_text('invalid')
            self.assertEqual(temperatures(root,root), [{'name':'cpu · Package','value':'42.0 °C'}])


if __name__ == '__main__':
    unittest.main()
