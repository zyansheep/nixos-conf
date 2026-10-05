"""Parquet tables derived from the power log: the cache behind the battery panel
and battery-eta, and the layer for analysis and model training.

The JSONL log (written every 10 s by waybar-monitor) stays the source of truth:
Parquet files are immutable and need a schema, so they suit finished data, not
a crash-safe append journal. Per day, under ~/.cache/waybar-monitor/parquet/:

    records/DAY.parquet      one row per 10 s record (flattened; map columns for
                             variable keys such as C-states, interrupts, settings)
    apps/DAY.parquet         one row per record × app
    minutes/DAY.parquet      per-minute aggregates (what the models train on)
    minute_apps/DAY.parquet  per minute × app: load (CPU s × GHz²), GPU, video
    events/DAY.parquet       collector starts and suspend gaps

Finished (compressed) days are converted once. Today's tables are extended from
the byte offset of the last complete minute (kept in meta/DAY.json), so a
refresh parses only new lines. Delete the directory to rebuild everything.

    duckdb -c "select avg(bat_w) from '~/.cache/waybar-monitor/parquet/records/*.parquet'"
"""
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

import report

ROOT = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'waybar-monitor/parquet'
VERSION = 1
F, I, S, B = pa.float64(), pa.int64(), pa.string(), pa.bool_()
MAP_F, MAP_I, MAP_S = pa.map_(S, F), pa.map_(S, I), pa.map_(S, S)

RECORDS = pa.schema([
    ('t0', F), ('t1', F), ('dt', F), ('boot', S), ('ac', B), ('status', S),
    ('bat_w', F), ('bat_min', F), ('bat_max', F), ('bat_samples', I), ('pct', F), ('charge', F),
    ('charge_unit', S), ('volts', F), ('soc_w', F), ('soc_min', F), ('soc_max', F),
    ('gpu_busy', F), ('gpu_s', F), ('video_s', F),
    ('cpu_user_s', F), ('cpu_sys_s', F), ('cpu_irq_s', F), ('cpu_iowait_s', F),
    ('cpu_busy_s', F), ('mhz', F), ('mhz_max', F), ('ctxt', I), ('intr', I),
    ('cstates', MAP_F), ('irq', MAP_I), ('net_rx', MAP_I), ('net_tx', MAP_I), ('wifi_dbm', MAP_F),
    ('disk_read', MAP_I), ('disk_write', MAP_I), ('disk_busy', MAP_F),
    ('usb', pa.list_(S)), ('pci', MAP_I), ('audio_play', I), ('audio_rec', I),
    ('bl', F), ('kbd', F), ('abm', I), ('outputs', S), ('niri', S), ('panel', S), ('settings', MAP_S),
    ('temp', F), ('fan', F), ('lid', S), ('locked', B),
    ('exp_run', S), ('exp_name', S), ('exp_block', I), ('exp_arm', S), ('exp_value', S), ('exp_washout', B),
])
APPS = pa.schema([('t1', F), ('boot', S), ('app', S), ('processes', I), ('rss_mib', F), ('cpu_s', F),
                  ('gpu_s', F), ('video_s', F), ('io_read', I), ('io_write', I)])
SETTINGS = ('profile', 'aspm', 'psr', 'boost', 'abm', 'wifi_ps', 'hz')
MINUTES = pa.schema(
    [('t', F), ('dt', F), ('st', S), ('boot', S)] + [(name, F) for name in report.NUMERIC]
    + [('pct', F), ('charge', F), ('volts', F), ('unit', S)]
    + [('set_profile', S), ('set_aspm', S), ('set_psr', B), ('set_boost', S), ('set_abm', I),
       ('set_wifi_ps', S), ('set_hz', F)]
    + [('exp_run', S), ('exp_name', S), ('exp_block', I), ('exp_arm', S), ('exp_washout', B), ('exp_value', S)])
MINUTE_APPS = pa.schema([('t', F), ('app', S), ('load', F), ('gpu', F), ('video', F)])
EVENTS = pa.schema([('event', S), ('t0', F), ('t1', F), ('boot', S), ('gap_s', F)]
                   + [(f'{side}_{field}', kind) for side in ('before', 'after')
                      for field, kind in (('pct', F), ('charge', F), ('volts', F), ('unit', S))])
TABLES = {'records': RECORDS, 'apps': APPS, 'minutes': MINUTES, 'minute_apps': MINUTE_APPS, 'events': EVENTS}


# --- Flattening --------------------------------------------------------------------

def pairs(mapping, cast=float):
    out = []
    for key, value in (mapping or {}).items():
        try:
            out.append((str(key), cast(value)))
        except (TypeError, ValueError):
            continue
    return out


def text(value):
    return None if value is None else value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def number(value, cast=float):
    try:
        return None if value is None else cast(value)
    except (TypeError, ValueError):
        return None


def record_row(r):
    bat, soc, gpu, cpu = r.get('bat', {}), r.get('soc', {}), r.get('gpu', {}), r.get('cpu', {})
    display, exp, audio = r.get('display', {}), r.get('exp') or {}, r.get('audio', {})
    net, disk = r.get('net', {}), r.get('disk', {})
    return {
        't0': r.get('t0'), 't1': r['t1'], 'dt': r['dt'], 'boot': r.get('boot'), 'ac': r.get('ac'),
        'status': r.get('status'), 'bat_w': bat.get('w'), 'bat_min': bat.get('min'), 'bat_max': bat.get('max'),
        'bat_samples': bat.get('n'), 'pct': bat.get('pct'), 'charge': bat.get('charge'),
        'charge_unit': bat.get('unit'), 'volts': bat.get('volts'),
        'soc_w': soc.get('w'), 'soc_min': soc.get('min'), 'soc_max': soc.get('max'),
        'gpu_busy': gpu.get('busy'), 'gpu_s': gpu.get('gpu'), 'video_s': gpu.get('video'),
        'cpu_user_s': cpu.get('user'), 'cpu_sys_s': cpu.get('sys'), 'cpu_irq_s': cpu.get('irq'),
        'cpu_iowait_s': cpu.get('iowait'), 'cpu_busy_s': cpu.get('busy_s'),
        'mhz': cpu.get('mhz'), 'mhz_max': cpu.get('mhz_max'), 'ctxt': number(cpu.get('ctxt'), int),
        'intr': number(cpu.get('intr'), int),
        'cstates': pairs(cpu.get('idle') if isinstance(cpu.get('idle'), dict) else {}),
        'irq': pairs(r.get('irq'), int),
        'net_rx': pairs({k: v.get('rx', 0) for k, v in net.items()}, int),
        'net_tx': pairs({k: v.get('tx', 0) for k, v in net.items()}, int),
        'wifi_dbm': pairs(r.get('wifi_dbm')),
        'disk_read': pairs({k: v.get('rd', 0) for k, v in disk.items()}, int),
        'disk_write': pairs({k: v.get('wr', 0) for k, v in disk.items()}, int),
        'disk_busy': pairs({k: v.get('busy', 0) for k, v in disk.items()}),
        'usb': [str(u) for u in r.get('usb', [])], 'pci': pairs(r.get('pci'), int),
        'audio_play': audio.get('play'), 'audio_rec': audio.get('rec'),
        'bl': display.get('bl'), 'kbd': display.get('kbd'), 'abm': number(display.get('abm'), int),
        'outputs': text(display.get('outputs')), 'niri': text(display.get('niri')), 'panel': text(display.get('panel')),
        'settings': [(k, text(v)) for k, v in (r.get('settings') or {}).items()],
        'temp': r.get('temp'), 'fan': r.get('fan'), 'lid': r.get('lid'), 'locked': r.get('locked'),
        'exp_run': exp.get('run'), 'exp_name': exp.get('name'), 'exp_block': exp.get('block'),
        'exp_arm': exp.get('arm'), 'exp_value': text(exp.get('value')), 'exp_washout': exp.get('washout'),
    }


def app_rows(r):
    return [{'t1': r['t1'], 'boot': r.get('boot'), 'app': name, 'processes': a.get('n'), 'rss_mib': a.get('rss'),
             'cpu_s': a.get('cpu'), 'gpu_s': a.get('gpu'), 'video_s': a.get('video'),
             'io_read': a.get('io_r'), 'io_write': a.get('io_w')} for name, a in r.get('apps', {}).items()]


def minute_row(m):
    row = {key: m.get(key) for key in ('t', 'dt', 'st', 'boot', *report.NUMERIC, 'pct', 'charge', 'volts', 'unit')}
    for key in SETTINGS:
        row[f'set_{key}'] = m.get('set', {}).get(key)
    row['set_abm'] = number(row['set_abm'], int)
    row['set_hz'] = number(row['set_hz'])
    exp = m.get('exp')
    if exp:
        row.update(exp_run=exp[0], exp_name=exp[1], exp_block=exp[2], exp_arm=exp[3], exp_washout=exp[4],
                   exp_value=text(exp[5]))
    return row


def minute_app_rows(m):
    return [{'t': m['t'], 'app': app, 'load': v[0], 'gpu': v[1], 'video': v[2]} for app, v in m.get('apps', {}).items()]


def event_row(e):
    row = {'event': e.get('event'), 't0': e.get('t0'), 't1': e.get('t1'), 'boot': e.get('boot'), 'gap_s': e.get('gap_s')}
    for side in ('before', 'after'):
        values = e.get(f'bat_{side}') or {}
        row.update({f'{side}_pct': values.get('pct'), f'{side}_charge': values.get('charge'),
                    f'{side}_volts': values.get('volts'), f'{side}_unit': values.get('unit')})
    return row


# --- Back to the dict rows report.py works with -----------------------------------------

def minutes_from(table_rows, app_table_rows):
    apps = {}
    for a in app_table_rows:
        apps.setdefault(a['t'], {})[a['app']] = [a['load'], a['gpu'], a['video']]
    out = []
    for row in table_rows:
        m = {'t': row['t'], 'dt': row['dt'], 'st': row['st']}
        for key in (*report.NUMERIC, 'pct', 'charge', 'volts', 'unit', 'boot'):
            if row.get(key) is not None:
                m[key] = row[key]
        if row['t'] in apps:
            m['apps'] = apps[row['t']]
        m['set'] = {key: row.get(f'set_{key}') for key in SETTINGS}
        if row.get('exp_run') is not None:
            value = row.get('exp_value')
            try:
                value = json.loads(value) if value is not None else None
            except ValueError:
                pass
            m['exp'] = [row['exp_run'], row['exp_name'], row['exp_block'], row['exp_arm'], row['exp_washout'], value]
        out.append(m)
    return out


def events_from(table_rows):
    out = []
    for row in table_rows:
        event = {k: row[k] for k in ('event', 't0', 't1', 'boot', 'gap_s') if row.get(k) is not None}
        for side in ('before', 'after'):
            values = {k: row[f'{side}_{k}'] for k in ('pct', 'charge', 'volts', 'unit') if row.get(f'{side}_{k}') is not None}
            if values:
                event[f'bat_{side}'] = values
        out.append(event)
    return out


# --- Files ----------------------------------------------------------------------------

def path(root, table, day):
    return root / table / f'{day}.parquet'


def write(root, day, tables):
    for name, rows in tables.items():
        target = path(root, name, day)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        pq.write_table(pa.Table.from_pylist(rows, schema=TABLES[name]), temporary, compression='zstd')
        temporary.replace(target)


def read(root, table, day, before=None, key='t'):
    file = path(root, table, day)
    if not file.exists():
        return []
    rows = pq.read_table(file).to_pylist()
    return rows if before is None else [r for r in rows if r[key] is not None and r[key] < before]


def build(records):
    records = list(records)
    minutes, events = report.aggregate(records)
    data = [r for r in records if 'event' not in r and isinstance(r.get('dt'), (int, float)) and 't1' in r]
    return {'records': [record_row(r) for r in data], 'apps': [row for r in data for row in app_rows(r)],
            'minutes': [minute_row(m) for m in minutes], 'minute_apps': [a for m in minutes for a in minute_app_rows(m)],
            'events': [event_row(e) for e in events]}, minutes, events


def load_day(day, log=report.LOG, root=ROOT):
    """Per-minute dict rows and events for one day, converting the log as needed."""
    sources = [p for p in (log / f'{day}.jsonl', log / f'{day}.jsonl.zst', log / f'{day}.jsonl.gz') if p.exists()]
    if not sources:
        return [], []
    source = sources[0]
    stamp = [source.name, source.stat().st_size, int(source.stat().st_mtime)]
    meta_path = root / 'meta' / f'{day}.json'
    try:
        meta = json.loads(meta_path.read_text())
        if meta.get('version') != VERSION or meta.get('source', [None])[0] != source.name:
            meta = None
    except (OSError, ValueError, AttributeError):
        meta = None
    if meta and meta['source'] == stamp:
        return (minutes_from(read(root, 'minutes', day), read(root, 'minute_apps', day)),
                events_from(read(root, 'events', day)))
    if source.suffix != '.jsonl':
        tables, minutes, events = build(report.read_records(source))
        write(root, day, tables)
        meta = {'version': VERSION, 'source': stamp}
    else:
        resume = meta if meta and meta.get('offset', 0) <= stamp[1] else {'offset': 0, 'cut': None}
        lines = list(report.read_from(source, resume['offset']))
        fresh, _, _ = build([record for _, record in lines])
        cut = resume.get('cut')
        tables = {}
        for name, fresh_rows in fresh.items():
            if name == 'events':  # Re-read events may already be stored: de-duplicate.
                seen, merged = set(), []
                for row in read(root, name, day) + fresh_rows:
                    if (row['event'], row['t1']) not in seen:
                        seen.add((row['event'], row['t1']))
                        merged.append(row)
                tables[name] = merged
            else:
                prior = read(root, name, day, cut, key='t1' if name in ('records', 'apps') else 't') if cut else []
                tables[name] = prior + fresh_rows
        newest = max((m['t'] for m in fresh['minutes']), default=None)
        offset = resume['offset']
        if newest is not None:
            # Next time, re-read from the first record of the newest (possibly
            # unfinished) minute and drop that minute's rows from these tables.
            offset = next((start for start, record in lines if 'event' not in record and 't1' in record
                           and int(record['t1'] // 60) * 60 == newest), offset)
            cut = newest
        write(root, day, tables)
        meta = {'version': VERSION, 'source': stamp, 'offset': offset, 'cut': cut}
        minutes = minutes_from(tables['minutes'], tables['minute_apps'])
        events = events_from(tables['events'])
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = meta_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(meta))
    temporary.replace(meta_path)
    return minutes, events
