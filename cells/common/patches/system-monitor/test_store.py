import json
import tempfile
import time
import unittest
from pathlib import Path

import pyarrow.parquet as pq

import report
import store
from test_report import record


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log, self.root = Path(self.tmp.name) / 'log', Path(self.tmp.name) / 'parquet'
        self.log.mkdir()
        self.day = time.strftime('%Y-%m-%d')
        base = int(time.mktime(time.strptime(self.day, '%Y-%m-%d'))) + 3600
        exp = {'run': 'r', 'name': 'aspm', 'block': 1, 'arm': 'B', 'value': 'powersupersave', 'washout': False}
        self.records = [record(base + 10 * i, bat=10 + i % 7, apps={'Floorp': {'cpu': 1.0, 'n': 3, 'rss': 500}},
                               exp=exp if i > 20 else None) for i in range(40)]
        self.records.insert(5, {'v': 1, 'event': 'start', 't1': base + 45, 'boot': 'b'})
        self.records.insert(30, {'v': 1, 'event': 'gap', 't0': base + 200, 't1': base + 300, 'gap_s': 3600,
                                 'bat_before': {'pct': 60, 'charge': 2e6, 'volts': 15.5, 'unit': 'uAh'},
                                 'bat_after': {'pct': 58, 'charge': 1.94e6}})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, mode='w'):
        with open(self.log / f'{self.day}.jsonl', mode) as handle:
            handle.write(''.join(json.dumps(r) + '\n' for r in records))

    def test_round_trip_matches_aggregate(self):
        self.write(self.records)
        minutes, events = store.load_day(self.day, self.log, self.root)
        expected_minutes, expected_events = report.aggregate(self.records)
        self.assertEqual(minutes, expected_minutes)
        self.assertEqual(report.sleep_drain(events), report.sleep_drain(expected_events))
        again, _ = store.load_day(self.day, self.log, self.root)  # Unchanged file: read from Parquet
        self.assertEqual(again, expected_minutes)

    def test_incremental_today_matches_full_parse_without_duplicates(self):
        self.write(self.records[:23])
        store.load_day(self.day, self.log, self.root)
        self.write(self.records[23:], mode='a')
        minutes, events = store.load_day(self.day, self.log, self.root)
        self.assertEqual(minutes, report.aggregate(self.records)[0])
        self.assertEqual(sorted(e['event'] for e in events), ['gap', 'start'])
        records = pq.read_table(self.root / 'records' / f'{self.day}.parquet').to_pylist()
        self.assertEqual(sorted(r['t1'] for r in records), sorted(r['t1'] for r in self.records if 'event' not in r))
        apps = pq.read_table(self.root / 'apps' / f'{self.day}.parquet').to_pylist()
        self.assertEqual((len(apps), apps[0]['app'], apps[0]['processes']), (40, 'Floorp', 3))


if __name__ == '__main__':
    unittest.main()
