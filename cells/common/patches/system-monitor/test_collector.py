import tempfile
import unittest
from pathlib import Path
from collector import History, processes, temperatures, TICKS, PAGE


class MonitorTests(unittest.TestCase):
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
