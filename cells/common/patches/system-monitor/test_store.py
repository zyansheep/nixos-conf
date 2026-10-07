import contextlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import store
from test_report import record


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log, self.db = Path(self.tmp.name) / 'power', Path(self.tmp.name) / 'power.sqlite3'
        self.log.mkdir()
        self.day = time.strftime('%Y-%m-%d')
        base = int(time.mktime(time.strptime(self.day, '%Y-%m-%d'))) + 3600
        exp = {'run': 'r', 'name': 'aspm', 'block': 1, 'arm': 'B', 'value': 'powersupersave', 'washout': False}
        self.records = [record(base + 10 * i, bat=10 + i % 7, apps={'Floorp': {'cpu': 1.0, 'n': 3, 'rss': 500},
                                                                   'idle': {'n': 2, 'rss': 80}},
                               exp=exp if i > 20 else None) for i in range(40)]
        self.records.insert(5, {'v': 1, 'event': 'start', 't1': base + 45, 'boot': 'b'})
        self.records.insert(30, {'v': 1, 'event': 'gap', 't0': base + 200, 't1': base + 300, 'gap_s': 3600,
                                 'bat_before': {'pct': 60, 'charge': 2e6, 'volts': 15.5, 'unit': 'uAh'},
                                 'bat_after': {'pct': 58, 'charge': 1.94e6}})
        self.span = (base - 3600, base + 7200)

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, records):
        (self.log / f'{self.day}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))

    def count(self, table):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_live_writes_match_aggregate_minute_by_minute(self):
        writer = store.Writer(self.db)
        for i, r in enumerate(self.records):
            writer.write(r)
            if i == 12:  # Mid-minute: the open minute already reflects what has landed.
                minutes, _ = store.load_range(*self.span, self.db)
                self.assertEqual(minutes, store.aggregate(self.records[:13])[0])
        writer.close()
        minutes, events = store.load_range(*self.span, self.db)
        self.assertEqual((minutes, events), store.aggregate(self.records))
        self.assertEqual(self.count('record_apps'), 40)  # Idle apps are left out.

    def test_import_is_idempotent_and_overlaps_live_writes(self):
        writer = store.Writer(self.db)
        for r in self.records[:23]:
            writer.write(r)
        writer.close()
        self.archive(self.records)
        self.assertEqual(store.import_logs(self.log, self.db), [])  # Written live: covered.
        self.assertEqual(store.import_logs(self.log, self.db, force=True), [self.day])
        self.assertEqual(store.import_logs(self.log, self.db, force=True), [self.day])
        self.assertEqual(store.load_range(*self.span, self.db), store.aggregate(self.records))
        self.assertEqual((self.count('records'), self.count('events')), (40, 2))

    def test_first_run_imports_the_archive(self):
        self.archive(self.records)
        self.assertEqual(store.import_logs(self.log, self.db), [self.day])
        self.assertEqual(store.import_logs(self.log, self.db), [])
        self.assertEqual(store.load_range(*self.span, self.db), store.aggregate(self.records))

    def test_live_writes_mark_the_day_so_restarts_skip_its_archive(self):
        self.archive(self.records)
        writer = store.Writer(self.db)
        writer.write(self.records[-1])
        writer.close()
        self.assertEqual(store.import_logs(self.log, self.db), [])

    def test_columns_keep_every_scalar(self):
        raw = dict(record(1000.0, apps={'nix': {'io_w': 4096}, 'idle': {'n': 1}}), temp=61.5, lid='open',
                   irq={'CAL': 9, 'LOC': 4, '126 mt7921e': 3, '99 nvme0q1': 2},
                   net={'wlan0': {'rx': 100, 'tx': 50}, 'tailscale0': {'rx': 7, 'tx': 1}},
                   settings={'platform_profile': 'balanced', 'nvme_apst_us': '0', 'wifi_ps': {'wlan0': 'on'}})
        raw['cpu']['idle'] = {'C3': 6.5}
        raw['display'].update(niri=[{'name': 'eDP-1', 'hz': 60.0, 'on': True}], panel={'night': True})
        row = store.record_row(raw)
        self.assertEqual((row['temp'], row['lid'], row['c3_s'], row['ipi_call'], row['irq_devices']),
                         (61.5, 'open', 6.5, 9, 5))
        self.assertEqual((row['wifi_rx'], row['wifi_tx'], row['net_rx']), (100, 50, 7))
        self.assertEqual((row['profile'], row['nvme_apst_us'], row['wifi_ps'], row['hz'], row['night_light']),
                         ('balanced', '0', 'on', 60.0, True))
        self.assertEqual(store.active_apps(raw), [('nix', None, None, None, None, 4096)])

    def test_reading_without_a_database_is_empty(self):
        self.assertEqual(store.load_range(*self.span, self.db), ([], []))
        self.assertFalse(self.db.exists())

    def test_an_older_layout_migrates_and_recomputes_minutes(self):
        writer = store.Writer(self.db)
        for r in self.records:
            writer.write(r)
        writer.close()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE minutes SET settings = '{}'")  # As layout 1 left them: no APST.
            conn.execute('PRAGMA user_version = 1')
            conn.commit()
        store.connect(self.db).close()
        minutes, _ = store.load_range(*self.span, self.db)
        self.assertEqual(minutes, store.aggregate(self.records)[0])
        self.assertIn('apst', minutes[0]['set'])

    def test_a_newer_layout_is_refused_not_dropped(self):
        writer = store.Writer(self.db)
        writer.write(self.records[0])
        writer.close()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(f'PRAGMA user_version = {store.VERSION + 1}')
        with self.assertRaises(sqlite3.DatabaseError):
            store.connect(self.db)
        self.assertEqual(self.count('records'), 1)


if __name__ == '__main__':
    unittest.main()
