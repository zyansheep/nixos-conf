import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import power
import report
import store
from test_report import record

# Columns each old layout's tables didn't have yet (later migrations added them):
# (records, minutes). Layouts 2 and 3 had layout 1's and 4's tables; imports
# lacked `records` and there was no meta table before 9.
LACKED = {
    1: ({'qol', 'cpu_max_mhz', 'gpu_dpm', 'throttle', 'mhz_active', 'active_n', 'min_mhz'},
        {'qol', 'present', 'ghz', 'ghz_active', 'purity'}),
    4: ({'cpu_max_mhz', 'gpu_dpm', 'throttle', 'mhz_active', 'active_n', 'min_mhz'}, {'ghz', 'ghz_active', 'purity'}),
    7: ({'mhz_active', 'active_n', 'min_mhz'}, {'ghz', 'ghz_active', 'purity'}),
    8: ({'mhz_active', 'active_n', 'min_mhz'}, {'ghz_active', 'purity'}),
}


def old_layout(path, version, records):
    """A database as layout `version` left it: its own tables (without the columns
    it lacked), its records, and minutes from its older Minute (no settings)."""
    lacked_records, lacked_minutes = LACKED[version]
    columns = [name for name, _, _ in store.COLUMNS if name not in lacked_records]
    kinds = {name: kind for name, kind, _ in store.COLUMNS}
    minutes = [name for name in store.MINUTE_COLUMNS if name not in lacked_minutes]
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute(f"CREATE TABLE records (id INTEGER PRIMARY KEY, {', '.join(f'{c} {kinds[c]}' for c in columns)})")
        for statement in store.SCHEMA:
            if statement.startswith(('CREATE TABLE app_names', 'CREATE TABLE record_apps', 'CREATE TABLE events',
                                     'CREATE INDEX events_t1')):
                conn.execute(statement)
        conn.execute(f"CREATE TABLE minutes ({', '.join(f'{c} ' + store.MINUTE_TYPES.get(c, 'REAL') for c in minutes)}, "
                     'apps TEXT, settings TEXT, exp TEXT)')
        conn.execute('CREATE TABLE imports (day TEXT PRIMARY KEY, at REAL NOT NULL)')
        ids = {}
        for r in records:
            if 'event' in r:
                conn.execute(store.INSERT_EVENT, (r['event'], r['t1'], r.get('t0'), r.get('boot'), r.get('gap_s'),
                                                  json.dumps(r)))
                continue
            row = store.record_row(r)
            cursor = conn.execute(f"INSERT INTO records ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                                  [row[c] for c in columns])
            for name, *values in store.active_apps(r):
                if name not in ids:
                    ids[name] = conn.execute('INSERT INTO app_names (name) VALUES (?)', (name,)).lastrowid
                conn.execute(store.INSERT_APP, (cursor.lastrowid, ids[name], *values))
        for m in store.aggregate(records)[0]:
            conn.execute(f"INSERT INTO minutes ({', '.join(minutes)}, apps, settings) VALUES "
                         f"({', '.join('?' * (len(minutes) + 2))})",
                         [m.get(c) for c in minutes] + [json.dumps(m.get('apps')), '{}'])
        conn.execute(f'PRAGMA user_version = {version}')
        conn.commit()


def columns(db, table):
    with contextlib.closing(sqlite3.connect(db)) as conn:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def finish(writer):
    steps = 0
    while writer.catch_up():
        steps += 1
    return steps


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
        for r in self.records:
            writer.write(r)
        writer.close()
        self.assertEqual(store.import_logs(self.log, self.db), [])  # No index: nothing says it lacks any.
        self.assertEqual(store.import_logs(self.log, self.db, counts={self.day: 40}), [])  # It has them all.

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

    def test_votes_are_kept_and_minutes_say_whether_any_was_a_thumbs_down(self):
        voted = [dict(r, v=3, qol=1 if i == 3 else -1 if i == 4 else 0) if 'event' not in r else r
                 for i, r in enumerate(self.records)]
        writer = store.Writer(self.db)
        for r in voted:
            writer.write(r)
        writer.close()
        minutes, _ = store.load_range(*self.span, self.db)
        self.assertEqual(minutes, store.aggregate(voted)[0])
        self.assertEqual(minutes[0]['qol'], -1)  # 👍 and 👎 in one minute: the 👎 counts.
        self.assertEqual(minutes[1]['qol'], 0)

    def test_a_newer_layout_is_refused_not_dropped(self):
        writer = store.Writer(self.db)
        writer.write(self.records[0])
        writer.close()
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute(f'PRAGMA user_version = {store.VERSION + 1}')
        with self.assertRaises(sqlite3.DatabaseError):
            store.connect(self.db)
        self.assertEqual(self.count('records'), 1)


    def test_genuinely_old_layouts_migrate_then_recompute_minutes_lazily(self):
        # The old chain recomputed minutes mid-way with today's Minute: layout 1
        # failed (KeyError 'qol'), layout 4 too (minutes had no column ghz).
        later = record(self.span[0] + 3600 + 3 * 60 + 5, bat=9)
        for version in LACKED:
            with self.subTest(layout=version):
                db = Path(self.tmp.name) / f'layout{version}.sqlite3'
                old_layout(db, version, self.records)
                self.assertNotIn('ghz_active', store.load_range(*self.span, db)[0][0])  # Readable before migrating.
                store.connect(db).close()  # Schema changes only: quick, no minute is recomputed yet.
                self.assertTrue({'mhz_active', 'active_n', 'min_mhz', 'qol', 'throttle'} <= columns(db, 'records'))
                self.assertTrue({'ghz', 'ghz_active', 'purity', 'qol', 'present'} <= columns(db, 'minutes'))
                self.assertIn('records', columns(db, 'imports'))
                self.assertFalse(store.minutes_ready(db))
                self.assertEqual(store.load_range(*self.span, db)[0][0]['set'], {})  # Still the old minute.
                writer = store.Writer(db)
                self.assertTrue(writer.write(later))  # Sampling goes on while minutes are stale.
                self.assertEqual(finish(writer), 0)  # One day of data: one step.
                writer.close()
                self.assertTrue(store.minutes_ready(db))
                self.assertEqual(store.load_range(*self.span, db)[0], store.aggregate(self.records + [later])[0])
                with contextlib.closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], store.VERSION)

    def test_minute_recompute_goes_a_day_at_a_time_newest_first_beside_live_writes(self):
        base = self.span[0] + 3600
        days = [record(base - offset + 10 * i, bat=10 + i) for offset in (4 * 86400, 86400, 0) for i in range(12)]
        db = Path(self.tmp.name) / 'old.sqlite3'
        old_layout(db, 8, days)
        writer = store.Writer(db)
        live = record(base + 115, bat=30)  # Lands in the open minute before the recompute reaches it.
        self.assertTrue(writer.write(live))
        expected = store.aggregate(days + [live])[0]
        self.assertTrue(writer.catch_up())  # Today's minutes (with the live record), in one transaction.
        minutes = store.load_range(-1e18, 1e18, db)[0]
        self.assertEqual(minutes[-2:], expected[-2:])
        self.assertEqual(minutes[0]['set'], {})  # The oldest day is still stale.
        self.assertFalse(store.minutes_ready(db))
        later = record(base + 125, bat=31)
        self.assertTrue(writer.write(later))  # The open minute again: the live refresh stays current.
        self.assertEqual(finish(writer), 1)  # Yesterday, then 4 days ago: the empty days in between are skipped.
        writer.close()
        self.assertTrue(store.minutes_ready(db))
        self.assertEqual(store.load_range(-1e18, 1e18, db)[0], store.aggregate(days + [live, later])[0])

    def test_minutes_ready_needs_the_current_layout_and_nothing_pending(self):
        self.assertFalse(store.minutes_ready(self.db))  # No database yet.
        writer = store.Writer(self.db)
        writer.write(self.records[0])
        self.assertTrue(store.minutes_ready(self.db))  # Created fresh: nothing to recompute.
        self.assertFalse(writer.catch_up())
        writer.close()
        old = Path(self.tmp.name) / 'old.sqlite3'
        old_layout(old, 7, self.records)
        self.assertFalse(store.minutes_ready(old))  # Not migrated yet.

    def test_reading_minutes_without_apps(self):
        writer = store.Writer(self.db)
        for r in self.records:
            writer.write(r)
        writer.close()
        full, events = store.load_range(*self.span, self.db)
        lean, same = store.load_range(*self.span, self.db, apps=False)
        self.assertTrue(any('apps' in m for m in full))
        self.assertEqual(lean, [{k: v for k, v in m.items() if k != 'apps'} for m in full])
        self.assertEqual(same, events)
        old = Path(self.tmp.name) / 'old.sqlite3'
        old_layout(old, 1, self.records)  # An older layout reads too.
        self.assertNotIn('apps', store.load_range(*self.span, old, apps=False)[0][0])

    def test_a_mixed_status_minute_keeps_the_majority_status_watts_and_says_how_pure_it_is(self):
        t = self.span[0] + 3600 * 2
        records = [record(t + 10, bat=12, soc=6), record(t + 20, bat=14, soc=8),
                   record(t + 30, bat=-30, soc=9, status='Charging'), record(t + 40, bat=10, soc=7)]
        minute = store.aggregate(records)[0][0]
        self.assertEqual((minute['st'], minute['purity']), ('D', 0.75))
        self.assertEqual((minute['bat'], minute['soc']), (12.0, 7.0))  # Not (12 + 14 − 30 + 10) / 4.
        writer = store.Writer(self.db)
        for r in records:
            writer.write(r)
        writer.close()
        self.assertEqual(store.load_range(*self.span, self.db)[0], [minute])
        self.assertEqual(store.aggregate(records[:2])[0][0]['purity'], 1.0)

    def test_minutes_average_the_active_clock_where_records_have_it(self):
        t = self.span[0] + 3600 * 3
        records = [record(t + 10, mhz=1500), record(t + 20, mhz=1500), record(t + 30, mhz=1500)]
        records[0]['cpu'].update(mhz_active=3000, active_n=2.5)
        records[1]['cpu'].update(mhz_active=2000, active_n=1.0)
        records[0]['settings'] = {'min_mhz': 406}
        row = store.record_row(records[0])
        self.assertEqual((row['mhz_active'], row['active_n'], row['min_mhz']), (3000, 2.5, 406))
        minute = store.aggregate(records)[0][0]
        self.assertEqual((minute['ghz'], minute['ghz_active']), (1.5, 2.5))  # Over the two records that had it.
        self.assertNotIn('ghz_active', store.aggregate(records[2:])[0][0])  # NULL, not 0, without it.

    def test_busy_zero_and_a_missing_clock(self):
        t = self.span[0] + 3600 * 4
        idle = record(t + 10, busy=0, apps={'Floorp': {'cpu': 0.0, 'gpu': 0.5}})
        unclocked = record(t + 70, busy=1, mhz=None, apps={'Floorp': {'cpu': 1.0}})
        first, second = store.aggregate([idle, unclocked])[0]
        self.assertEqual((first['busy'], first['load']), (0.0, 0.0))
        self.assertEqual(first['apps'], {'Floorp': [0.0, 0.05, 0.0]})
        self.assertEqual(second['busy'], 1.0)
        for key in ('load', 'ghz', 'ghz_active'):
            self.assertNotIn(key, second)
        self.assertEqual(second['apps'], {'Floorp': [0.0, 0.0, 0.0]})  # Format unchanged: [load, gpu, video].
        writer = store.Writer(self.db)
        for r in (idle, unclocked):
            self.assertTrue(writer.write(r))
        writer.close()
        self.assertEqual(store.load_range(t, t + 3600, self.db)[0], [first, second])

    def test_a_failed_write_is_read_back_from_the_archive_once_writes_work(self):
        records = [r for r in self.records if 'event' not in r][:30]
        self.archive(records)
        writer = store.Writer(self.db)
        real = store.insert
        failing = {3, 4, 5, 10, 11, 12, 13, 20, 21, 22}

        def insert(conn, batch, ids):
            if batch and batch[0] is records_by_t1.get(batch[0].get('t1')) and order[batch[0]['t1']] in failing:
                raise sqlite3.OperationalError('database is locked')
            return real(conn, batch, ids)
        records_by_t1 = {r['t1']: r for r in records}
        order = {r['t1']: i for i, r in enumerate(records)}
        with mock.patch('store.insert', insert), contextlib.redirect_stderr(io.StringIO()) as err:
            results = [writer.write(r) for r in records]
        self.assertEqual(results.count(False), 10)
        self.assertIn('database is locked', err.getvalue())
        self.assertEqual(self.count('records'), 20)
        self.assertEqual(writer.missed, {self.day})
        while writer.catch_up(self.log):  # Re-read: idempotent for the 20 it had.
            pass
        self.assertEqual(writer.missed, set())
        writer.close()
        self.assertEqual(self.count('records'), 30)
        self.assertEqual(store.load_range(*self.span, self.db)[0], store.aggregate(records)[0])

    def test_a_restart_re_reads_a_day_the_database_lacks_records_of(self):
        # An old build refused by the newer layout, then the collector restarts:
        # 10 of 30 records only in the archive.
        records = [r for r in self.records if 'event' not in r][:30]
        self.archive(records)
        writer = store.Writer(self.db)
        with contextlib.redirect_stderr(io.StringIO()):
            for i, r in enumerate(records):
                if i == 10:
                    writer.close()
                    with contextlib.closing(sqlite3.connect(self.db)) as conn:
                        conn.execute(f'PRAGMA user_version = {store.VERSION + 1}')
                if i == 20:
                    with contextlib.closing(sqlite3.connect(self.db)) as conn:
                        conn.execute(f'PRAGMA user_version = {store.VERSION}')
                writer.write(r)
        writer.close()
        self.assertEqual(self.count('records'), 20)
        (self.log / 'index.json').write_text(json.dumps({'days': {self.day: {'records': 30}}}))
        self.assertEqual(store.import_logs(self.log, self.db), [self.day])
        self.assertEqual(self.count('records'), 30)
        self.assertEqual(store.import_logs(self.log, self.db), [])  # Has them all now.
        self.assertEqual(store.load_range(*self.span, self.db)[0], store.aggregate(records)[0])
        # Records the database can never take (a duplicate t1 in the archive) don't
        # make every restart re-read the day.
        self.archive(records + [records[-1]])
        self.assertEqual(store.import_logs(self.log, self.db, counts={self.day: 31}), [self.day])
        self.assertEqual(store.import_logs(self.log, self.db, counts={self.day: 31}), [])

    def test_unexpected_errors_never_escape_the_writer_or_imports(self):
        writer = store.Writer(self.db)
        with mock.patch('store.insert', side_effect=ValueError('not sqlite')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(writer.write(self.records[0]))
        self.assertIn('ValueError: not sqlite', err.getvalue())
        self.assertTrue(writer.write(self.records[0]))
        writer.close()
        (self.log / '2000-01-01.jsonl.zst').write_bytes(b'\x28\xb5\x2f\xfd' + b'garbage' * 50)
        self.archive(self.records)
        self.db.unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(store.import_logs(self.log, self.db), ['2000-01-01', self.day])
        self.assertIn('2000-01-01.jsonl.zst', err.getvalue())
        self.assertEqual(self.count('records'), 40)

    def test_collector_and_panel_attribution_agree(self):
        # The collector splits each 10 s record (power.attribute); the panel splits
        # the minute the store makes of them (report.attribute). Over a steady
        # minute they must tell the same story.
        t = self.span[0] + 3600 * 5
        records = []
        for i in range(6):
            r = record(t + 10 * (i + 1), bat=20.0, soc=10.0, busy=1.0, mhz=2000,
                       apps={'Browser': {'cpu': 6.0}, 'Game': {'cpu': 2.0, 'gpu': 2.0}})
            r['gpu'] = {'gpu': 2.0, 'video': 0}
            records.append(r)
        params = dict(power.PRIOR)
        joules = {}
        for r in records:
            for name, value in power.attribute(r, params).items():
                joules[name] = joules.get(name, 0) + value
        minute = store.aggregate(records)[0][0]
        parts = report.attribute(minute, params)
        for app in ('Browser', 'Game', 'Kernel'):
            self.assertAlmostEqual(joules[app] / 60, parts['apps'][app], places=3)
        self.assertAlmostEqual(joules['Processor baseline'] / 60, parts['groups']['Processor baseline'], places=3)
        self.assertAlmostEqual(sum(joules.values()) / 60, sum(parts['groups'].values()), places=3)

if __name__ == '__main__':
    unittest.main()
