"""The power database: what the battery models, panel and battery-eta use, written
by the collector as it measures. Standard library only (the collector runs on
plain Python).

~/.local/state/waybar-monitor/power.sqlite3:

    records      one row per 10 s record: every scalar the log has — battery and
                 chip power, CPU time and frequency, C-state residency, IPIs,
                 GPU/video time, network, disk, devices, display, power
                 settings, thermals, experiment arm — as typed columns, NULL
                 where absent
    record_apps  one row per record × app that did something (CPU, GPU, video
                 or disk IO); app names are interned in `app_names`
    events       collector starts and suspend gaps (the full event as JSON)
    minutes      per-minute aggregates the models train on, recomputed from
                 `records` whenever one lands (apps/settings/exp as JSON)
    imports      archive days the database already has: read in, or written live
    meta         `stale_before`: minutes before it await a recompute after a
                 migration changed Minute (the collector works through them a
                 day at a time; minutes_ready() says when they are done)

The JSONL log (`power/DAY.jsonl[.zst]`) is still written alongside as a raw
archive: it alone has idle apps' process counts and memory, per-interrupt and
per-interface detail and display layouts. On startup the collector imports any
archive day the database has never seen (the first run, or after deleting it)
or holds fewer records of than the archive (a write failed); `waybar-monitor
import` re-reads every day, keeping what is already stored.

    sqlite3 ~/.local/state/waybar-monitor/power.sqlite3 "select avg(bat_w) from records where status = 'Discharging'"
    duckdb -c "select * from sqlite_scan('~/.local/state/waybar-monitor/power.sqlite3', 'records')"
"""
import collections
import contextlib
import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    from compression import zstd
except ImportError:  # Python < 3.14
    zstd = None

STATE = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'waybar-monitor'
DB = STATE / 'power.sqlite3'
VERSION = 9  # PRAGMA user_version; MIGRATIONS below. Migrate, never drop: this is primary data.
NUMERIC = ('bat', 'soc', 'load', 'busy', 'gpu', 'video', 'bl', 'kbd', 'wifi', 'disk', 'usb', 'audio', 'ghz',
           'ghz_active')
BY_STATUS = ('bat', 'soc')  # Averaged over the minute's majority status only (signed watts don't mix).
STATUS = {'Discharging': 'D', 'Charging': 'C', 'Not charging': 'F', 'Full': 'F'}
CHUNK = 86400  # Seconds of stale minutes recomputed per transaction after a migration.


# --- Raw log record → columns ------------------------------------------------------------

def part(record, section):
    value = record.get(section)
    return value if isinstance(value, dict) else {}


def net_sum(record, key, wifi):
    return sum(n.get(key, 0) for name, n in part(record, 'net').items()
               if isinstance(n, dict) and name.startswith('wl') == wifi and name != 'lo')


def edp(record):
    return next((o for o in part(record, 'display').get('niri') or []
                 if isinstance(o, dict) and str(o.get('name', '')).startswith('eDP')), {})


def irq_devices(record):
    return sum(v for k, v in part(record, 'irq').items() if k[:1].isdigit())


def text(value):
    return None if value is None else json.dumps(value)


def flag(value):
    return None if value is None else bool(value)


# (column, SQL type, value from a raw log record). The log format is in power.py.
COLUMNS = (
    ('v', 'INTEGER', lambda r: r.get('v')),
    ('t0', 'REAL', lambda r: r.get('t0')),
    ('t1', 'REAL NOT NULL UNIQUE', lambda r: r['t1']),
    ('dt', 'REAL NOT NULL', lambda r: r['dt']),
    ('boot', 'TEXT', lambda r: r.get('boot')),
    ('ac', 'INTEGER', lambda r: flag(r.get('ac'))),
    ('status', 'TEXT', lambda r: r.get('status')),
    # Battery draw (W, + discharging) over the record, ~1 Hz samples, and its state
    ('bat_w', 'REAL', lambda r: part(r, 'bat').get('w')),
    ('bat_min', 'REAL', lambda r: part(r, 'bat').get('min')),
    ('bat_max', 'REAL', lambda r: part(r, 'bat').get('max')),
    ('bat_n', 'INTEGER', lambda r: part(r, 'bat').get('n')),
    ('pct', 'REAL', lambda r: part(r, 'bat').get('pct')),
    ('charge', 'REAL', lambda r: part(r, 'bat').get('charge')),
    ('unit', 'TEXT', lambda r: part(r, 'bat').get('unit')),
    ('volts', 'REAL', lambda r: part(r, 'bat').get('volts')),
    # Chip package power (amdgpu PPT, W), ~4 Hz samples
    ('soc_w', 'REAL', lambda r: part(r, 'soc').get('w')),
    ('soc_min', 'REAL', lambda r: part(r, 'soc').get('min')),
    ('soc_max', 'REAL', lambda r: part(r, 'soc').get('max')),
    ('soc_n', 'INTEGER', lambda r: part(r, 'soc').get('n')),
    # CPU: seconds summed over cores; MHz mean/max over the record's samples
    ('busy_s', 'REAL', lambda r: part(r, 'cpu').get('busy_s')),
    ('user_s', 'REAL', lambda r: part(r, 'cpu').get('user')),
    ('sys_s', 'REAL', lambda r: part(r, 'cpu').get('sys')),
    ('irq_s', 'REAL', lambda r: part(r, 'cpu').get('irq')),
    ('iowait_s', 'REAL', lambda r: part(r, 'cpu').get('iowait')),
    ('steal_s', 'REAL', lambda r: part(r, 'cpu').get('steal')),
    ('mhz', 'REAL', lambda r: part(r, 'cpu').get('mhz')),
    ('mhz_max', 'REAL', lambda r: part(r, 'cpu').get('mhz_max')),
    ('ctxt', 'INTEGER', lambda r: part(r, 'cpu').get('ctxt')),
    ('intr', 'INTEGER', lambda r: part(r, 'cpu').get('intr')),
    # Idle-state residency, seconds summed over cores
    ('c_poll_s', 'REAL', lambda r: part(part(r, 'cpu'), 'idle').get('POLL')),
    ('c1_s', 'REAL', lambda r: part(part(r, 'cpu'), 'idle').get('C1')),
    ('c2_s', 'REAL', lambda r: part(part(r, 'cpu'), 'idle').get('C2')),
    ('c3_s', 'REAL', lambda r: part(part(r, 'cpu'), 'idle').get('C3')),
    # Interrupts: inter-processor (call, timer, TLB shootdown, reschedule) and the
    # top device lines the log keeps
    ('ipi_call', 'INTEGER', lambda r: part(r, 'irq').get('CAL')),
    ('ipi_timer', 'INTEGER', lambda r: part(r, 'irq').get('LOC')),
    ('ipi_tlb', 'INTEGER', lambda r: part(r, 'irq').get('TLB')),
    ('ipi_resched', 'INTEGER', lambda r: part(r, 'irq').get('RES')),
    ('irq_devices', 'INTEGER', irq_devices),
    # GPU: busy fraction, and engine-seconds summed over clients
    ('gpu_busy', 'REAL', lambda r: part(r, 'gpu').get('busy')),
    ('gpu_s', 'REAL', lambda r: part(r, 'gpu').get('gpu')),
    ('video_s', 'REAL', lambda r: part(r, 'gpu').get('video')),
    # Network bytes (Wi-Fi vs everything else) and signal
    ('wifi_rx', 'INTEGER', lambda r: net_sum(r, 'rx', True)),
    ('wifi_tx', 'INTEGER', lambda r: net_sum(r, 'tx', True)),
    ('net_rx', 'INTEGER', lambda r: net_sum(r, 'rx', False)),
    ('net_tx', 'INTEGER', lambda r: net_sum(r, 'tx', False)),
    ('wifi_dbm', 'REAL', lambda r: max(part(r, 'wifi_dbm').values(), default=None)),
    # Storage (bytes, busy seconds) and devices
    ('disk_read', 'INTEGER', lambda r: sum(d.get('rd', 0) for d in part(r, 'disk').values())),
    ('disk_write', 'INTEGER', lambda r: sum(d.get('wr', 0) for d in part(r, 'disk').values())),
    ('disk_busy', 'REAL', lambda r: sum(d.get('busy', 0) for d in part(r, 'disk').values())),
    ('usb', 'INTEGER', lambda r: len(r.get('usb') or [])),
    ('pci_active', 'INTEGER', lambda r: sum(part(r, 'pci').values())),
    ('audio_play', 'INTEGER', lambda r: part(r, 'audio').get('play')),
    ('audio_rec', 'INTEGER', lambda r: part(r, 'audio').get('rec')),
    # Display
    ('bl', 'REAL', lambda r: part(r, 'display').get('bl')),
    ('kbd', 'REAL', lambda r: part(r, 'display').get('kbd')),
    ('abm', 'INTEGER', lambda r: part(r, 'display').get('abm')),
    ('hz', 'REAL', lambda r: edp(r).get('hz')),
    ('screen_on', 'INTEGER', lambda r: flag(edp(r).get('on'))),
    ('night_light', 'INTEGER', lambda r: flag(part(part(r, 'display'), 'panel').get('night'))),
    ('grayscale', 'INTEGER', lambda r: flag(part(part(r, 'display'), 'panel').get('grayscale'))),
    # Power settings
    ('profile', 'TEXT', lambda r: part(r, 'settings').get('platform_profile')),
    ('epp', 'TEXT', lambda r: part(r, 'settings').get('epp')),
    ('governor', 'TEXT', lambda r: part(r, 'settings').get('governor')),
    ('boost', 'TEXT', lambda r: part(r, 'settings').get('boost')),
    ('amd_pstate', 'TEXT', lambda r: part(r, 'settings').get('amd_pstate')),
    ('max_mhz', 'REAL', lambda r: part(r, 'settings').get('max_mhz')),
    ('cpu_max_mhz', 'REAL', lambda r: part(r, 'settings').get('cpu_max_mhz')),
    ('gpu_dpm', 'TEXT', lambda r: part(r, 'settings').get('gpu_dpm')),
    ('throttle', 'TEXT', lambda r: part(r, 'settings').get('throttle')),
    ('aspm', 'TEXT', lambda r: part(r, 'settings').get('aspm')),
    ('nvme_apst_us', 'TEXT', lambda r: part(r, 'settings').get('nvme_apst_us')),
    ('dcdebugmask', 'TEXT', lambda r: part(r, 'settings').get('dcdebugmask')),
    ('wifi_ps', 'TEXT', lambda r: ','.join(sorted(part(part(r, 'settings'), 'wifi_ps').values())) or None),
    ('rfkill_bluetooth', 'TEXT', lambda r: part(r, 'settings').get('rfkill_bluetooth')),
    ('rfkill_wlan', 'TEXT', lambda r: part(r, 'settings').get('rfkill_wlan')),
    ('charge_limit', 'REAL', lambda r: part(r, 'settings').get('charge_limit')),
    # Thermals and session
    ('temp', 'REAL', lambda r: r.get('temp')),
    ('fan', 'REAL', lambda r: r.get('fan')),
    ('lid', 'TEXT', lambda r: r.get('lid')),
    ('locked', 'INTEGER', lambda r: flag(r.get('locked'))),
    # power-experiment arm
    ('exp_run', 'TEXT', lambda r: part(r, 'exp').get('run')),
    ('exp_name', 'TEXT', lambda r: part(r, 'exp').get('name')),
    ('exp_block', 'INTEGER', lambda r: part(r, 'exp').get('block')),
    ('exp_arm', 'TEXT', lambda r: part(r, 'exp').get('arm')),
    ('exp_value', 'TEXT', lambda r: text(part(r, 'exp').get('value'))),
    ('exp_washout', 'INTEGER', lambda r: part(r, 'exp').get('washout', True) if r.get('exp') else None),
    # Quality-of-life votes in the record (qol.py): −1 a 👎, +1 a 👍, 0 none
    # (schema 2 logged a 0–3 level instead, which nothing reads)
    ('qol', 'INTEGER', lambda r: r.get('qol') if (r.get('v') or 0) >= 3 else None),
    # The clock of the CPUs that were doing something: amd-pstate reports an idle
    # CPU at exactly scaling_min_freq, a placeholder, so `mhz` mixes in the floor.
    # mhz_active: time-weighted mean MHz over CPUs reading above their minimum (not
    # the collector's own); active_n: the mean count of such CPUs; min_mhz: cpu0's
    # scaling_min_freq, that placeholder (it follows the platform profile)
    ('mhz_active', 'REAL', lambda r: part(r, 'cpu').get('mhz_active')),
    ('active_n', 'REAL', lambda r: part(r, 'cpu').get('active_n')),
    ('min_mhz', 'REAL', lambda r: part(r, 'settings').get('min_mhz')),
)
NAMES = [name for name, _, _ in COLUMNS]
APP_FIELDS = (('cpu_s', 'cpu'), ('gpu_s', 'gpu'), ('video_s', 'video'), ('io_read', 'io_r'), ('io_write', 'io_w'))


def is_record(record):
    return 'event' not in record and isinstance(record.get('dt'), (int, float)) and 't1' in record


def record_row(record):
    return {name: get(record) for name, _, get in COLUMNS}


def active_apps(record):
    """(app, cpu_s, gpu_s, video_s, io_read, io_write) for apps that did something."""
    out = []
    for name, app in part(record, 'apps').items():
        values = tuple(app.get(key) or None for _, key in APP_FIELDS) if isinstance(app, dict) else ()
        if any(values):
            out.append((name, *values))
    return out


# --- Per-minute aggregates -----------------------------------------------------------------

def psr_enabled(mask):
    """PSR on/off from amdgpu.dcdebugmask. Records before it was logged ran with
    nixos-hardware's 0x10 on every boot, so a missing value means off."""
    try:
        return not int(mask, 0) & 0x10
    except (TypeError, ValueError):
        return False


def minute_of(t1):
    return int(t1 // 60) * 60


def day_of(t1):
    return datetime.date.fromtimestamp(t1).isoformat()  # The archive's file day (power.PowerLog).


class Minute:
    """dt-weighted means over one minute's records (as `records` rows). `st` is
    the status most of the minute had and `purity` that share of it; `bat` and
    `soc` average only the records in that status."""

    def __init__(self, t):
        # sums and weights by key, or by (status, key) for BY_STATUS keys.
        self.t, self.dt, self.sums, self.weights = t, 0.0, collections.defaultdict(float), collections.defaultdict(float)
        self.status, self.apps, self.last, self.exp, self.settings = collections.Counter(), {}, {}, None, {}
        self.boot, self.qol, self.present = None, None, 0.0

    def add(self, row, apps):
        """`apps`: (name, cpu_s, gpu_s, video_s, …) for the record's active apps."""
        dt = row['dt']
        if dt <= 0:
            return
        self.dt += dt
        status = STATUS.get(row['status'], 'U')
        self.status[status] += dt
        mhz, active = row['mhz'], row.get('mhz_active')
        ghz2 = (mhz / 1000) ** 2 if mhz else None
        busy = (row['busy_s'] or 0) / dt
        values = {
            'bat': row['bat_w'], 'soc': row['soc_w'], 'busy': busy, 'load': busy * ghz2 if ghz2 else None,
            'gpu': (row['gpu_s'] or 0) / dt, 'video': (row['video_s'] or 0) / dt, 'bl': row['bl'], 'kbd': row['kbd'],
            'wifi': ((row['wifi_rx'] or 0) + (row['wifi_tx'] or 0)) / dt / 1e6,
            'disk': min(1.0, (row['disk_busy'] or 0) / dt), 'usb': row['usb'] or 0,
            'audio': min(1, (row['audio_play'] or 0) + (row['audio_rec'] or 0)),
            'ghz': mhz / 1000 if mhz else None,  # Mean core clock, busy or not: the chip model's clock term.
            'ghz_active': active / 1000 if active else None,  # Mean clock of the CPUs above their idle floor.
        }
        for key, value in values.items():
            if value is not None:
                slot = (status, key) if key in BY_STATUS else key
                self.sums[slot] += value * dt
                self.weights[slot] += dt
        for name, cpu, gpu, video, *_ in apps:
            if not (cpu or gpu or video):
                continue
            entry = self.apps.setdefault(name, [0.0, 0.0, 0.0])
            entry[0] += (cpu or 0) * (ghz2 or 0)
            entry[1] += gpu or 0
            entry[2] += video or 0
        for key in ('pct', 'charge', 'volts', 'unit'):
            if row[key] is not None:
                self.last[key] = row[key]
        self.boot = row['boot']
        if row['qol'] is not None:  # −1 if any 👎, else +1 for a 👍, else 0.
            self.qol = row['qol'] if self.qol is None else -1 if -1 in (self.qol, row['qol']) else max(self.qol, row['qol'])
        if not row['locked'] and row['screen_on'] is not False and row['screen_on'] != 0:
            self.present += dt  # Someone could be using it: unlocked, screen on.
        self.settings = {'profile': row['profile'], 'aspm': row['aspm'], 'psr': psr_enabled(row['dcdebugmask']),
                         'boost': row['boost'], 'abm': row['abm'], 'wifi_ps': row['wifi_ps'], 'hz': row['hz'],
                         'apst': row['nvme_apst_us'], 'mhz': row['max_mhz'], 'cpu_mhz': row.get('cpu_max_mhz'),
                         'gpu_dpm': row.get('gpu_dpm'), 'throttle': row.get('throttle')}  # mhz: the frequency cap; cpu_mhz: the CPU's maximum (boost).
        if row['exp_run'] is not None:
            exp = [row['exp_run'], row['exp_name'], row['exp_block']]
            washout = bool(row['exp_washout']) or (self.exp is not None and self.exp[4])
            if self.exp is not None and self.exp[:3] != exp:
                washout = True  # Two blocks inside one minute.
            value = json.loads(row['exp_value']) if row['exp_value'] is not None else None
            self.exp = [*exp, row['exp_arm'], washout, value]

    def row(self):
        status = self.status.most_common(1)[0][0]
        row = {'t': self.t, 'dt': round(self.dt, 2), 'st': status}
        for key in NUMERIC:
            slot = (status, key) if key in BY_STATUS else key
            if self.weights.get(slot):
                row[key] = round(self.sums[slot] / self.weights[slot], 4)
        if self.apps:
            row['apps'] = {name: [round(v / self.dt, 4) for v in values] for name, values in self.apps.items()}
        row.update(self.last)
        row['set'] = self.settings
        if self.boot:
            row['boot'] = self.boot
        if self.qol is not None:
            row['qol'] = self.qol
        row['present'] = round(self.present / self.dt, 2)
        row['purity'] = round(self.status[status] / self.dt, 4)  # Share of the minute in its status `st`.
        if self.exp is not None:
            row['exp'] = self.exp
        return row


def aggregate(records):
    """Raw log records → (per-minute rows, events), both sorted by time."""
    minutes, events = {}, []
    for record in records:
        if 'event' in record:
            events.append(record)
        elif is_record(record):
            t = minute_of(record['t1'])
            minutes.setdefault(t, Minute(t)).add(record_row(record), active_apps(record))
    rows = [m.row() for t, m in sorted(minutes.items()) if m.dt > 0]
    return rows, sorted(events, key=lambda e: e.get('t1', 0))


# --- Database --------------------------------------------------------------------------------

MINUTE_COLUMNS = ('t', 'dt', 'st', 'boot', *NUMERIC, 'pct', 'charge', 'volts', 'unit', 'qol', 'present', 'purity')
MINUTE_TYPES = {'t': 'REAL PRIMARY KEY', 'st': 'TEXT', 'boot': 'TEXT', 'unit': 'TEXT'}  # The rest REAL.
APP_TYPES = {'io_read': 'INTEGER', 'io_write': 'INTEGER'}  # The rest REAL.
RECORD_SQL = ', '.join(f'{name} {kind}' for name, kind, _ in COLUMNS)
APP_SQL = ', '.join(f"{column} {APP_TYPES.get(column, 'REAL')}" for column, _ in APP_FIELDS)
MINUTE_SQL = ', '.join(f"{column} {MINUTE_TYPES.get(column, 'REAL')}" for column in MINUTE_COLUMNS)
META_SQL = 'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value)'
SCHEMA = [
    f'CREATE TABLE records (id INTEGER PRIMARY KEY, {RECORD_SQL})',
    'CREATE TABLE app_names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)',
    'CREATE TABLE record_apps (record INTEGER NOT NULL REFERENCES records(id), '
    f'app INTEGER NOT NULL REFERENCES app_names(id), {APP_SQL}, PRIMARY KEY (record, app)) WITHOUT ROWID',
    'CREATE TABLE events (id INTEGER PRIMARY KEY, event TEXT NOT NULL, t1 REAL NOT NULL, t0 REAL, boot TEXT, '
    'gap_s REAL, data TEXT NOT NULL, UNIQUE (event, t1))',
    'CREATE INDEX events_t1 ON events (t1)',
    # A minute row is ~1.5 KB (mostly `apps`): a rowid table packs that far
    # better than WITHOUT ROWID would.
    f'CREATE TABLE minutes ({MINUTE_SQL}, apps TEXT, settings TEXT, exp TEXT)',
    # records: how many records the archive day held when it was last read in
    # (NULL: written live), so a day is re-read only when the archive has grown.
    'CREATE TABLE imports (day TEXT PRIMARY KEY, at REAL NOT NULL, records INTEGER)',
    META_SQL,
]


def add_column(conn, table, column, kind):
    if column not in {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')


def add_qol(conn):
    add_column(conn, 'records', 'qol', 'INTEGER')
    add_column(conn, 'minutes', 'qol', 'REAL')
    add_column(conn, 'minutes', 'present', 'REAL')


def votes_only(conn):
    conn.execute('UPDATE records SET qol = NULL WHERE v IS NULL OR v < 3')


def add_levers(conn):
    add_column(conn, 'records', 'cpu_max_mhz', 'REAL')
    add_column(conn, 'records', 'gpu_dpm', 'TEXT')


def add_throttle(conn):
    add_column(conn, 'records', 'throttle', 'TEXT')


def add_clock(conn):
    add_column(conn, 'minutes', 'ghz', 'REAL')


def add_active_clock(conn):
    for column in ('mhz_active', 'active_n', 'min_mhz'):
        add_column(conn, 'records', column, 'REAL')
    add_column(conn, 'minutes', 'ghz_active', 'REAL')
    add_column(conn, 'minutes', 'purity', 'REAL')
    add_column(conn, 'imports', 'records', 'INTEGER')
    conn.execute(META_SQL)


# MIGRATIONS[n] takes layout n to n + 1: (schema change or None, whether Minute
# changed). Schema changes run at once, in connect(). Minutes are derived, so a
# Minute change only marks them stale, once, after the whole chain (against the
# final layout); the collector then recomputes them a day per transaction
# (Writer.catch_up) while it samples.
MIGRATIONS = {
    1: (None, True),  # Minute settings gained NVMe APST.
    2: (add_qol, True),  # Records and minutes gained quality of life; minutes the share of time someone was there.
    3: (votes_only, True),  # Quality of life became 👍/👎 votes; drop the 0–3 levels schema 2 logged.
    4: (None, True),  # Minute settings gained the CPU frequency cap (boost as it really is).
    5: (add_levers, False),  # Records gained the CPU's own maximum (boost under a cap) and the amdgpu DPM level.
    6: (add_throttle, False),  # Records gained whether app-throttle ran.
    7: (add_clock, True),  # Minutes gained the mean core clock (chip power rises with it even when idle).
    # Records gained the active-CPU clock (mhz_active, active_n) and the idle floor
    # (min_mhz); minutes ghz_active, `purity`, and bat/soc of the majority status
    # only; imports the archive's record count; and the meta table.
    8: (add_active_clock, True),
}


def connect(db=DB):
    """A read-write connection, creating or migrating the database on first use."""
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] != VERSION:
            with transaction(conn):  # Re-read under the write lock: another process may have done it.
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                if version > VERSION:
                    raise sqlite3.DatabaseError(f'{db} has layout {version}, this build only knows up to {VERSION}')
                if version == 0:
                    for statement in SCHEMA:
                        conn.execute(statement)
                elif version < VERSION:
                    stale = False
                    for step in range(version, VERSION):
                        change, minutes = MIGRATIONS[step]
                        if change is not None:
                            change(conn)
                        stale |= minutes
                    if stale:
                        mark_stale(conn)
                conn.execute(f'PRAGMA user_version = {VERSION}')
    except BaseException:
        conn.close()
        raise
    return conn


@contextlib.contextmanager
def transaction(conn):
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


# --- Deferred minute recomputes ----------------------------------------------------------------
# A migration that changes Minute sets meta `stale_before` past the newest minute:
# every minute before it was computed by an older Minute. recompute_step() then
# recomputes the newest CHUNK below it and moves it down, one transaction each,
# until nothing older is left and the key goes. A live write recomputes its own
# minute meanwhile. Both read `records` and replace minutes inside one write
# transaction, so whichever runs last leaves the same, current rows: they cannot
# undo each other (nor can `waybar-monitor import` in another process).

def mark_stale(conn):
    conn.execute(META_SQL)
    top = conn.execute('SELECT max(x) FROM (SELECT max(t1) AS x FROM records UNION ALL '
                       'SELECT max(t) FROM minutes)').fetchone()[0]
    if top is not None:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('stale_before', ?)", (minute_of(top) + 60,))


def recompute_step(conn, span=CHUNK):
    """Recompute the newest `span` seconds of stale minutes in one transaction.
    Returns whether stale minutes remain."""
    with transaction(conn):
        found = conn.execute("SELECT value FROM meta WHERE key = 'stale_before'").fetchone()
        if found is None:
            return False
        start = found[0] - span
        refresh_minutes(conn, start, found[0])
        below = conn.execute('SELECT max(x) FROM (SELECT max(t1) AS x FROM records WHERE t1 < ? UNION ALL '
                             'SELECT max(t) FROM minutes WHERE t < ?)', (start, start)).fetchone()[0]
        if below is None:
            conn.execute("DELETE FROM meta WHERE key = 'stale_before'")
            return False
        conn.execute("UPDATE meta SET value = ? WHERE key = 'stale_before'", (minute_of(below) + 60,))
        return True


def minutes_ready(db=DB):
    """Whether every minute is current: the database has this build's layout (or
    newer) and no recompute is pending. Read-only and cheap. False while the
    collector is still working through a migration's recompute (older minutes
    then lack new columns or hold old values), and when there is no database."""
    try:
        conn = sqlite3.connect(Path(db).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    except sqlite3.OperationalError:
        return False
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] < VERSION:
            return False
        return conn.execute("SELECT 1 FROM meta WHERE key = 'stale_before'").fetchone() is None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def compact(value):
    return json.dumps(value, separators=(',', ':'))


INSERT_RECORD = f"INSERT OR IGNORE INTO records ({', '.join(NAMES)}) VALUES ({', '.join('?' * len(NAMES))})"
INSERT_APP = f"INSERT OR IGNORE INTO record_apps VALUES ({', '.join('?' * (2 + len(APP_FIELDS)))})"
INSERT_EVENT = 'INSERT OR IGNORE INTO events (event, t1, t0, boot, gap_s, data) VALUES (?, ?, ?, ?, ?, ?)'
INSERT_MINUTE = (f"INSERT INTO minutes ({', '.join(MINUTE_COLUMNS)}, apps, settings, exp) "
                 f"VALUES ({', '.join('?' * (len(MINUTE_COLUMNS) + 3))})")


def app_id(conn, ids, name):
    if name not in ids:
        found = conn.execute('SELECT id FROM app_names WHERE name = ?', (name,)).fetchone()
        ids[name] = found[0] if found else conn.execute('INSERT INTO app_names (name) VALUES (?)', (name,)).lastrowid
    return ids[name]


def insert(conn, records, ids):
    """Store raw log records (inside a transaction) and refresh their minutes.
    Records and events already stored are left alone. Returns how many records
    `records` held (stored now or before)."""
    touched, count = [], 0
    for record in records:
        if 'event' in record:
            conn.execute(INSERT_EVENT, (record.get('event'), record.get('t1', 0), record.get('t0'),
                                        record.get('boot'), record.get('gap_s'), compact(record)))
        elif is_record(record):
            count += 1
            row = record_row(record)
            cursor = conn.execute(INSERT_RECORD, [row[name] for name in NAMES])
            if cursor.rowcount:
                conn.executemany(INSERT_APP, [(cursor.lastrowid, app_id(conn, ids, name), *values)
                                              for name, *values in active_apps(record)])
                touched.append(minute_of(record['t1']))
    if touched:
        refresh_minutes(conn, min(touched), max(touched) + 60)
    return count


def refresh_minutes(conn, start, end):
    """Recompute every minute in [start, end) (minute-aligned) from `records`."""
    apps = collections.defaultdict(list)
    for record, name, *values in conn.execute(
            'SELECT a.record, n.name, a.cpu_s, a.gpu_s, a.video_s FROM record_apps a '
            'JOIN app_names n ON n.id = a.app JOIN records r ON r.id = a.record WHERE r.t1 >= ? AND r.t1 < ?',
            (start, end)):
        apps[record].append((name, *values))
    minutes = {}
    for row in conn.execute('SELECT * FROM records WHERE t1 >= ? AND t1 < ? ORDER BY t1', (start, end)):
        row = dict(row)
        t = minute_of(row['t1'])
        minutes.setdefault(t, Minute(t)).add(row, apps.get(row['id'], ()))
    conn.execute('DELETE FROM minutes WHERE t >= ? AND t < ?', (start, end))
    conn.executemany(INSERT_MINUTE, [minute_values(m.row()) for m in minutes.values() if m.dt > 0])


def minute_values(m):
    return ([m.get(name) for name in MINUTE_COLUMNS]
            + [compact(m['apps']) if m.get('apps') else None, compact(m.get('set', {})),
               compact(m['exp']) if m.get('exp') is not None else None])


def minute_from(row):
    """Back to the dict Minute.row() made (absent values stay absent)."""
    keys = row.keys()  # A reader can meet an older layout until the collector migrates it.
    m = {name: row[name] for name in MINUTE_COLUMNS if name in keys and row[name] is not None}
    if 'apps' in keys and row['apps'] is not None:
        m['apps'] = json.loads(row['apps'])
    m['set'] = json.loads(row['settings']) if row['settings'] is not None else {}
    if row['exp'] is not None:
        m['exp'] = json.loads(row['exp'])
    return m


def complain(where, error):
    print(f'waybar-monitor: {where}: {type(error).__name__}: {error}', file=sys.stderr)


class Writer:
    """The collector's side: one small transaction per record, every 10 s. Any
    error is reported and retried on the next record; it never stops the JSONL
    archive. A day whose write failed is re-read from the archive once writes
    work again (catch_up), as is, on a later start, any day the archive holds more
    records of (import_logs)."""

    def __init__(self, db=DB):
        self.db, self.conn, self.ids = Path(db), None, {}
        self.missed = set()  # Days with a record or event the database didn't take.
        self.stale = True  # Minutes may await a recompute (asked once per connection).

    def write(self, record):
        """Store one record or event; whether it worked."""
        try:
            if self.conn is None:
                self.conn, self.ids, self.stale = connect(self.db), {}, True
            with transaction(self.conn):
                insert(self.conn, [record], self.ids)
                # From now on this day is written live, so a restart needn't read
                # its archive file back in (unless the archive has more of it).
                if 't1' in record:
                    self.conn.execute('INSERT OR IGNORE INTO imports (day, at) VALUES (?, ?)',
                                      (day_of(record['t1']), time.time()))
            return True
        except Exception as error:
            complain(self.db, error)
            if isinstance(record.get('t1'), (int, float)):
                self.missed.add(day_of(record['t1']))
            self.close()
            return False

    def catch_up(self, log=None):
        """One piece of deferred work, between samples: re-read an archive day
        (`log` directory) a failed write missed, else recompute a day of stale
        minutes. Only on a connection the last write succeeded on. Returns
        whether there is more it could do now."""
        if self.conn is None:
            return False  # Writes don't work (yet): the next successful one reopens it.
        try:
            if self.missed and log is not None:
                # One try per miss (a record no write can take mustn't re-read its
                # day forever); a restart's import_logs catches what a try lost.
                day = min(self.missed)
                self.missed.discard(day)
                if (path := log_days(log).get(day)) is not None:
                    with transaction(self.conn):
                        insert(self.conn, read_records(path), self.ids)
                        self.conn.execute('INSERT OR IGNORE INTO imports (day, at) VALUES (?, ?)', (day, time.time()))
            elif self.stale:
                self.stale = recompute_step(self.conn)
        except Exception as error:
            complain(self.db, error)
            self.close()
        return bool(self.missed and log is not None) or self.stale

    def close(self):
        if self.conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self.conn.close()
        self.conn, self.ids = None, {}


# --- The JSONL archive -----------------------------------------------------------------------

def open_log(path):
    return zstd.open(path, 'rt') if path.suffix == '.zst' else open(path)


def read_records(path):
    try:
        with open_log(path) as source:
            for line in source:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # A torn final line after a crash.
    except (EOFError, ValueError):
        return  # A torn compressed file: the records so far still stand.
    except Exception as error:  # Unreadable or corrupt (ZstdError is a plain Exception): the rest stands.
        complain(path, error)


def log_days(log):
    days = {}
    for path in sorted(Path(log).glob('*.jsonl*')):
        if path.name[:4].isdigit() and path.suffix in ('.jsonl', '.zst'):
            days.setdefault(path.name.split('.')[0], path)  # .jsonl sorts before .jsonl.zst
    return days


def archive_counts(log):
    """Records per archive day, from the archive's index.json (power.PowerLog)."""
    try:
        days = json.loads((Path(log) / 'index.json').read_text())['days']
        return {day: int(stats['records']) for day, stats in days.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def day_span(day):
    """[start, end) wall seconds of a local calendar day (what day_of maps to it)."""
    date = datetime.date.fromisoformat(day)
    start = datetime.datetime.combine(date, datetime.time())
    return start.timestamp(), datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time()).timestamp()


def lacking(conn, day, archived, seen):
    """Whether the database lacks records of `day` the archive holds: it has
    `archived` (None: unknown) and held `seen` when last read in."""
    if archived is None or archived == seen:
        return False
    return conn.execute('SELECT count(*) FROM records WHERE t1 >= ? AND t1 < ?', day_span(day)).fetchone()[0] < archived


def import_logs(log, db=DB, force=False, counts=None):
    """Read archive days into the database: those never imported, those the
    archive holds more records of than the database (a write failed: a long lock
    wait, a full disk, a collector too old for the layout), or all with `force`.
    `counts`: records per archive day (the collector passes its PowerLog index;
    else index.json). Idempotent. Returns the days read."""
    counts = archive_counts(log) if counts is None else counts
    conn = connect(db)
    try:
        seen = {row[0]: row[1] for row in conn.execute('SELECT day, records FROM imports')}
        ids, read = {}, []
        for day, path in log_days(log).items():
            if day in seen and not force and not lacking(conn, day, counts.get(day), seen[day]):
                continue
            with transaction(conn):
                found = insert(conn, read_records(path), ids)
                conn.execute('INSERT OR REPLACE INTO imports (day, at, records) VALUES (?, ?, ?)',
                             (day, time.time(), counts.get(day, found)))
            read.append(day)
        return read
    finally:
        conn.close()


# --- Readers (the panel, battery-eta, analysis) -------------------------------------------------

def load_range(start, end, db=DB, apps=True):
    """Per-minute dict rows and events with start <= t < end. Read-only: an
    absent or not yet created database is just empty. `apps=False` leaves out
    each minute's 'apps' (neither read nor parsed: much faster)."""
    try:
        conn = sqlite3.connect(Path(db).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    except sqlite3.OperationalError:
        return [], []
    conn.row_factory = sqlite3.Row
    try:
        columns = '*'
        if not apps:
            columns = ', '.join(row[1] for row in conn.execute('PRAGMA table_info(minutes)') if row[1] != 'apps') or '*'
        minutes = [minute_from(row) for row in
                   conn.execute(f'SELECT {columns} FROM minutes WHERE t >= ? AND t < ? ORDER BY t', (start, end))]
        events = [json.loads(row['data']) for row in
                  conn.execute('SELECT data FROM events WHERE t1 >= ? AND t1 < ? ORDER BY t1', (start, end))]
        return minutes, events
    except sqlite3.OperationalError:  # No tables yet.
        return [], []
    finally:
        conn.close()
