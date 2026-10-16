#!/usr/bin/env python3
"""The battery panel's data: everything it shows for one period, computed off
the UI thread, and the text it puts in its header and rows. GTK-free, so
test_panel_data.py runs it headless on a synthetic month of every kind of
data, and `battery-panel-check` runs it on the real power database.

    battery-panel-check     (each period the panel shows: time per part, and errors)

Each part is computed on its own: one that fails falls back to an empty value,
its error goes in `errors` (with the traceback on stderr, so in the journal),
and the rest still shows. A failure in one analysis once left the whole panel
blank.
"""
import collections
import json
import math
import sys
import threading
import time
import traceback
from pathlib import Path

import eta  # First: it limits BLAS threads before numpy loads.
import experiment
import report
import store
from report import PHRASES, gain_text

FREEZES = ('freeze', 'freeze-apps')  # Paired A/B runs, analysed apart from the setting model.
HISTORY_DAYS = 30
BUDGET = 5.0  # s: battery-panel-check fails a period that takes longer to gather.
PERIODS = {'6 h': 6 * 3600, 'Day': 86400, 'Week': 7 * 86400}
MIN_ROWS = 30  # Battery minutes (report.battery_rows) report.what_if needs.
# A period with fewer borrows the latest FALLBACK_HOURS of them for the what-if.
FALLBACK_HOURS = 8
SPANS = 4  # Periods kept with their per-minute apps while the panel is open.
FRESH = 3600  # s: recommend.json and models.json older than this are stale (as the menu and power-experiment).
PICKER = 12  # Apps the "Close an app" picker lists (most CPU time first), plus the chosen one.
NOTE_SECONDS = 30  # A command's failure or refusal stays under the banner this long (or until the next command).


class Cancelled(BaseException):
    """A newer reload superseded this gather; raised between its parts (not an
    Exception, so `part` doesn't report it as a failure)."""


def load_minutes(start, end, apps=True):
    """Minutes and events in [start, end) from the power database. The history
    (`apps` false) without each minute's per-app JSON — decoding it for 30 days
    was 85% of opening the panel — and with experiment minutes' clean parts
    (report.load_range), which the settings models use, as battery-eta's do.
    A span with its apps (the timeline, the what-if) needs no clean parts."""
    if not apps:
        return report.load_range(start, end, store.DB, apps=False)
    return store.load_range(start, end, store.DB, apps=True)


def minutes_ready():
    """False while the store recomputes its minutes after a layout change (the
    first ~30–40 s after an update: the newest days come back first)."""
    ready = getattr(store, 'minutes_ready', None)
    return bool(ready(store.DB)) if callable(ready) else True


def top_up(old, since, new):
    """`old` minutes before `since`, then the `new` ones fetched from it."""
    return [m for m in old if m['t'] < since] + new


def new_events(old, new):
    known = {(e.get('event'), e.get('t1')) for e in old}
    return old + [e for e in new if (e.get('event'), e.get('t1')) not in known]


class History:
    """The last HISTORY_DAYS of minutes (without their apps) and events, and the
    last few spans loaded with their apps. While the panel is open, a reload
    (range switch, paging, the minute tick) fetches only minutes from the
    newest one on, which may still have been filling — except once the store
    has finished recomputing minutes after an update (`ready`): minutes loaded
    before that held old values, so everything is loaded again."""

    def __init__(self, load=None, clock=time.time, ready=None):
        self.load, self.clock, self.ready = load or load_minutes, clock, ready or minutes_ready
        self.lock = threading.Lock()
        self.minutes, self.events, self.since = [], [], None
        self.version, self.cache = None, {}
        self.loads = 0  # Full loads: part of `version`, so a reload after a recompute refits.
        self.store_ready, self.stale = True, False  # The store's minutes are current; ours predate that.
        self.spans = collections.OrderedDict()  # (start, end) → (minutes, events), with apps.
        self.rows = {}  # t → a minute with its apps, for rows_with_apps.

    def get(self):
        with self.lock:
            now = self.clock()
            start = now - HISTORY_DAYS * 86400
            try:
                ready = bool(self.ready())
            except Exception:  # noqa: BLE001 - unknown: neither reload nor say it is recomputing
                ready = not self.stale
            if self.since is None or (self.stale and ready):
                self.minutes, self.events = self.load(start, now + 60, apps=False)
                self.spans.clear()
                self.rows.clear()
                self.loads += 1
            else:
                minutes, events = self.load(self.since, now + 60, apps=False)
                self.minutes = [m for m in top_up(self.minutes, self.since, minutes) if m['t'] >= start]
                self.events = new_events([e for e in self.events if e.get('t1', 0) >= start], events)
            self.store_ready, self.stale = ready, not ready
            self.since = self.minutes[-1]['t'] if self.minutes else None
            self.version = (self.loads, len(self.minutes), self.since, len(self.events))
            return list(self.minutes), list(self.events)

    def with_apps(self, start, end):
        """Minutes and events in [start, end) with each minute's apps (the
        timeline splits power by app; the what-if closes them). The last SPANS
        spans are kept and topped up from their newest minute."""
        with self.lock:
            key = (start, end)
            if key in self.spans:
                minutes, events = self.spans.pop(key)
                since = minutes[-1]['t'] if minutes else start
                new, more = self.load(since, end, apps=True)
                minutes, events = top_up(minutes, since, new), new_events(events, more)
            else:
                minutes, events = self.load(start, end, apps=True)
            self.spans[key] = (minutes, events)
            while len(self.spans) > SPANS:
                self.spans.popitem(last=False)
            return list(minutes), list(events)

    def rows_with_apps(self, rows, gap=3 * 3600):
        """History `rows` (oldest first) again with their apps, fetched in a few
        ranges (split where rows are over `gap` s apart) and kept: a reload
        fetches only new ones, and the newest, which may still be filling."""
        with self.lock:
            missing = [m['t'] for m in rows if m['t'] not in self.rows] + ([rows[-1]['t']] if rows else [])
            ranges = []
            for t in sorted(set(missing)):
                if ranges and t - ranges[-1][1] <= gap:
                    ranges[-1][1] = t
                else:
                    ranges.append([t, t])
            wanted = set(missing)
            for first, last in ranges:
                self.rows.update((m['t'], m) for m in self.load(first, last + 60, apps=True)[0] if m['t'] in wanted)
            keep = {m['t'] for m in rows}
            self.rows = {t: m for t, m in self.rows.items() if t in keep}
            return [self.rows.get(m['t'], m) for m in rows]

    def memo(self, name, key, compute, versioned=True):
        """`compute()`, reused while `key` (and, if `versioned`, the history) is
        unchanged: paging and switching periods don't refit the history-wide
        models."""
        key = (self.version, key) if versioned else key
        if name in self.cache and self.cache[name][0] == key:
            return self.cache[name][1]
        value = compute()
        self.cache[name] = (key, value)
        return value


def no_levers():
    """lever_effects' result before any setting has been measured."""
    return {'effects': [], 'interactions': [], 'blocks': 0, 'current': {}, 'coef': {}, 'samples': {},
            'sigma': None, 'sigma_raw': None, 'progress': {}}


def finite(*values):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def typical_draw(shared, rows_all, model):
    """THE typical battery draw (W) the panel's gains are computed at, and what it
    is: the mean of the last 30 days' battery minutes at your settings
    (report.typical_power: each minute less what an experiment's settings
    added), battery-eta's (models.json `typical_power`, the draw recommend.json's
    gains are at too) while its models are fresh, else figured here. Not the
    ETA's `typical`, its level of the last ~5 h: a gain per full charge is for
    the long run."""
    typical = (shared or {}).get('typical_power')
    if finite(typical) and typical > 0:
        return float(typical), f"battery-eta's: the mean at your settings of the last {HISTORY_DAYS} days on battery"
    power = report.typical_power(rows_all, model) if rows_all and model else None
    if finite(power) and power > 0:
        return power, (f"mean at your settings of the last {HISTORY_DAYS} days' {len(rows_all):,} battery minutes, "
                       f"{days(rows_all[0]['t'], rows_all[-1]['t'])}")
    return None, ''


def models_age(now=None):
    """Seconds since battery-eta last wrote models.json, None if it never has."""
    try:
        return (now or time.time()) - eta.MODELS.stat().st_mtime
    except OSError:
        return None


def models_outdated():
    """Whether models.json is from another battery-eta version (eta.load_models
    ignores it): just after an update, until the new battery-eta's first refit."""
    try:
        found = json.loads(eta.MODELS.read_text()).get('version')
    except (OSError, ValueError, AttributeError):
        return False
    return found != getattr(eta, 'MODELS_VERSION', found)


def read_recommend(path=None, now=None):
    """battery-eta's recommend.json while under FRESH seconds old, else None."""
    try:
        data = json.loads(Path(path or experiment.RECOMMEND).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not finite(data.get('updated')) or (now or time.time()) - data['updated'] > FRESH:
        return None
    return data


def recommendations(levers, quality, energy, typical, exclude=(), recommend=None):
    """Each goal's best combination from ONE source, and which: battery-eta's
    recommend.json while fresh — what the battery menu shows and the
    Battery/Comfort switch applies — else ('panel') computed here as battery-eta
    does, from every vote in the history, leaving `exclude`
    (experiment.unrecommended: every setting outside the pool) as it is."""
    if recommend:
        return {goal: recommend[goal] for goal in report.GOALS if isinstance(recommend.get(goal), dict)}, 'battery-eta'
    if quality is None:
        return {}, None
    return report.recommendations(levers, quality, energy, typical, exclude=tuple(exclude)), 'panel'


def quarantined():
    """Settings the runner's watchdog quarantined after a hardware incident: {name: why}."""
    return {str(name): str(why.get('reason') or '') if isinstance(why, dict) else str(why or '')
            for name, why in experiment.quarantined().items()}


def gather(start, end, history=None, on_period=None, cancelled=None):
    """Everything the panel shows for [start, end). Every key is always there;
    `errors` maps each part that failed to its error, `timings` each part to
    seconds. Only an unreadable database gives `error` alone; None when
    `cancelled()` turned true between two parts (a newer reload).

    The period's own parts (timeline, sleep) come first and, if given, go to
    `on_period` (with `pending` set) while the rest — the what-if, and the
    experiments, fitted on all HISTORY_DAYS with gains at the typical draw —
    are computed, or reused from `history` when it hasn't changed."""
    errors, timings = {}, {}
    began = time.perf_counter()
    history = history or History()

    def part(name, compute, fallback):
        if cancelled is not None and cancelled():
            raise Cancelled
        started = time.perf_counter()
        try:
            return compute()
        except Exception as error:  # noqa: BLE001 - reported, and the rest still shows
            errors[name] = f'{type(error).__name__}: {error}'
            print(f'battery-panel: {name} failed', file=sys.stderr)
            traceback.print_exc()
            return fallback
        finally:
            timings[name] = round(time.perf_counter() - started, 3)

    try:
        return collect(start, end, history, on_period, part, errors, timings, began)
    except Cancelled:
        return None


def collect(start, end, history, on_period, part, errors, timings, began):
    loaded = part('history', history.get, None)
    if loaded is None:
        return {'error': f"Couldn't read the power database: {errors['history']}", 'errors': errors,
                'timings': timings}
    minutes_all, events = loaded
    # Battery minutes, experiments' included (but their washout): the models hold settings at yours.
    rows_all = report.battery_rows(minutes_all)
    # battery-eta trains every model; fit here only if its models.json is missing or stale.
    shared = part('models', eta.load_models, None)
    model = part('battery model', lambda: shared['attribution'] if shared else history.memo(
        'battery model', None, lambda: report.fit_rows(rows_all, minutes=minutes_all)), None)
    minutes, older = part('period', lambda: history.with_apps(start, end), ([], []))
    around = minutes_all  # Whether each suspend began and ended on battery (sleep_drain).
    if start < time.time() - HISTORY_DAYS * 86400:
        events = new_events(older, events)
        around = sorted({m['t']: m for m in minutes + minutes_all}.values(), key=lambda m: m['t'])
    period_events = [e for e in events if start - 86400 <= e.get('t1', 0) < end + 86400]
    energy = part('energy', lambda: report.full_energy(minutes_all, reserve_wh=eta.reserve_wh()), None)
    typical, typical_note = part('typical draw', lambda: history.memo(
        'typical', ((shared or {}).get('fitted'), (shared or {}).get('typical')),
        lambda: typical_draw(shared, rows_all, model)), (None, ''))
    result = dict(points=part('timeline', lambda: report.timeline(minutes, model) if model else [], []),
                  span=(start, end), sleeps=part('sleep drain', lambda: [
                      s for s in report.sleep_drain(period_events, around) if s['end'] > start and s['start'] < end], []),
                  sleeps_all=part('sleep history', lambda: report.sleep_drain(events, minutes_all), []), estimates=[],
                  info=None, scope=None, energy=energy, power=typical, typical=typical, typical_note=typical_note,
                  model=model, trained=shared.get('fitted') if shared else None, errors=errors, timings=timings)
    if on_period:
        on_period(dict(result, pending=True, errors=dict(errors)))

    # The what-if: this period's battery minutes, or the latest hours of them.
    rows = report.battery_rows(minutes)
    scope = 'this period'
    if len(rows) < MIN_ROWS:
        latest = rows_all[-FALLBACK_HOURS * 60:]
        scope, rows = 'latest', report.battery_rows(part('latest battery time',
                                                         lambda: history.rows_with_apps(latest), []))
    key = (scope, len(rows), rows and rows[0]['t'], rows and rows[-1]['t'], len(rows_all),
           rows_all and rows_all[-1]['t'], energy, result['trained'], model is None, history.loads)
    estimates, info = part('apps and display', lambda: history.memo(
        'what-if', key, lambda: report.what_if(rows, energy, model=model, train_rows=rows_all), versioned=False)
        if rows and model else ([], None), ([], None))
    result.update(estimates=estimates, info=info, scope=scope_text(scope, rows),
                  power=info['power'] if info else typical)

    pool = part('experiment pool', experiment.pool, [])
    cached = lambda name, compute, *key: history.memo(name, (energy, typical, *key), compute)
    levers = part('settings model', lambda: cached('levers', lambda: report.lever_effects(minutes_all, energy, typical)),
                  no_levers())
    # App freezing runs only while locked, so it stays a separate paired A/B analysis.
    freezes = part('app freezing', lambda: cached('freezes', lambda: [
        e for e in report.experiment_effects(minutes_all, energy, typical) if e['experiment'] in FREEZES]
        if typical else []), [])
    plan = part('experiment plan', lambda: cached('plan', lambda: report.plan(minutes_all, events, pool)
                                                  if pool else None, tuple(pool)), None)
    # Every vote counts, with or without a pool: as battery-eta's recommend.json.
    quality = part('quality of life', lambda: plan['qol'] if plan else cached(
        'qol', lambda: report.qol_model(minutes_all, events)), None)
    quarantine = part('quarantine', quarantined, {})
    unrecommended = part('unrecommended', experiment.unrecommended, ())
    recommend = part('recommend.json', read_recommend, None)
    recs, source = part('recommendations', lambda: cached('recs', lambda: recommendations(
        levers, quality, energy, typical, exclude=unrecommended, recommend=recommend), tuple(unrecommended),
        recommend), ({}, None))
    result.update(levers=levers, effects=freezes, pool=pool, plan=plan, quality=quality, recs=recs, recs_source=source,
                  goal=part('goal', experiment.goal, 'battery'), quarantine=quarantine,
                  risky=tuple(getattr(experiment, 'RISKY', ()) or ()),
                  psr=part('panel self-refresh', lambda: cached('psr', lambda: report.boot_effect(minutes_all, energy,
                                                                                                   typical)), None),
                  psr_arm=part('boot arm', experiment.psr_arm, None),
                  catalog=part('settings', lambda: [experiment.describe(spec)
                                                    for spec in experiment.catalog().values()], []),
                  units=part('app scopes', experiment.freezable_units, []))  # All: the picker keeps the chosen one.
    result['health'] = dict(battery=len(rows_all), tested=sum(1 for m in rows_all if m.get('exp')),
                            first=rows_all[0]['t'] if rows_all else None,
                            last=rows_all[-1]['t'] if rows_all else None, used=len(rows),
                            model={k: (model or {}).get(k) for k in ('rows', 'experiment_rows', 't0', 't1', 'kept',
                                                                     'prior')},
                            used_first=rows[0]['t'] if rows else None, used_last=rows[-1]['t'] if rows else None,
                            trained=result['trained'], models_age=models_age(), blocks=levers.get('blocks'),
                            models_outdated=part('models version', models_outdated, False) if not shared else False,
                            levers_span=span_of(levers), ready=getattr(history, 'store_ready', True))
    result['seconds'] = round(time.perf_counter() - began, 3)
    return result


def span_of(data):
    """(first, last) epoch seconds a result says it covers, if it says."""
    for first, last in (('first', 'last'), ('start', 'end')):
        if finite(data.get(first), data.get(last)):
            return data[first], data[last]
    span = data.get('span') or data.get('range')
    return tuple(span) if isinstance(span, (list, tuple)) and len(span) == 2 and finite(*span) else None


# --- Text ---------------------------------------------------------------------------

def duration(hours):
    """'2 h 05 m', or '45 min' under an hour; rounded to the minute first, so
    1.9925 h is '2 h 00 m' (not '1 h 60 m')."""
    if not finite(hours):
        return '--'
    minutes = round(max(0.0, hours) * 60)
    return f'{minutes // 60} h {minutes % 60:02d} m' if minutes >= 60 else f'{minutes} min'


def long_duration(hours):
    """As duration, in whole hours from ten up ('23 h')."""
    return f'{round(hours)} h' if finite(hours) and hours >= 10 else duration(hours)


def days(first, last):
    """'Oct 3–11', 'Oct 30 – Nov 2' or 'Oct 15'."""
    a, b = time.localtime(first), time.localtime(last)
    if (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday):
        return time.strftime('%b %-d', a)
    if (a.tm_year, a.tm_mon) == (b.tm_year, b.tm_mon):
        return f"{time.strftime('%b %-d', a)}–{b.tm_mday}"
    return f"{time.strftime('%b %-d', a)} – {time.strftime('%b %-d', b)}"


def ago(seconds):
    """'under a minute', '12 min', '5 h' or '3 days'."""
    return ('under a minute' if seconds < 60 else f'{seconds / 60:.0f} min' if seconds < 5400
            else f'{seconds / 3600:.0f} h' if seconds < 172800 else f'{seconds / 86400:.0f} days')


def tested(rows):
    return sum(1 for m in rows if m.get('exp'))


def scope_text(scope, rows):
    """What the what-if was computed on, with its dates: battery minutes,
    experiments' included (the models hold settings at yours)."""
    if not rows:
        return f'no battery time in the last {HISTORY_DAYS} days'
    amount = (f"{duration(len(rows) / 60)} on battery ({days(rows[0]['t'], rows[-1]['t'])})"
              + (f', {duration(tested(rows) / 60)} of it in experiments' if tested(rows) else ''))
    if scope == 'this period':
        return f"this period's {amount}"
    return f'the latest {amount} (this period has under {MIN_ROWS} minutes on battery)'


def model_text(model, same=None):
    """The battery model's data health (report.fit_rows' keys), if it has any;
    `same`: (minutes, in experiments) the line already gave."""
    model = model or {}
    if model.get('prior'):
        return f'prior values (under {report.MIN_ROWS} battery minutes to fit)'
    if not finite(model.get('rows')) or not model['rows']:
        return ''
    if same == (model['rows'], model.get('experiment_rows') or 0) and not model.get('kept'):
        return 'on those minutes'
    text = f"{int(model['rows']):,} minutes"
    if finite(model.get('t0'), model.get('t1')):
        text += f" ({days(model['t0'], model['t1'])})"
    if finite(model.get('experiment_rows')) and model['experiment_rows']:
        text += f", {int(model['experiment_rows']):,} in experiments"
    return f'its last fit, kept: {text} (too few new battery minutes)' if model.get('kept') else text


def health_text(health, now=None):
    """The data-health line: the battery minutes behind the what-if and the
    typical draw (experiments' included), the battery model's own data, the
    settings model's blocks, and how fresh the models are."""
    now = now or time.time()
    health = health or {}
    if health.get('battery'):
        parts = [f"{health['battery']:,} battery minutes in the last {HISTORY_DAYS} days "
                 f"({days(health['first'], health['last'])})"
                 + (f", {health['tested']:,} of them in experiments" if health.get('tested') else '')
                 + (f"; the what-if used {health['used']:,} ({days(health['used_first'], health['used_last'])})"
                    if health.get('used') else '')]
    else:
        parts = [f'no battery minutes in the last {HISTORY_DAYS} days, so no what-if']
    model = model_text(health.get('model'), (health.get('battery'), health.get('tested') or 0))
    age = health.get('models_age')
    if health.get('trained'):
        fitted = (f"trained {time.strftime('%H:%M', time.localtime(health['trained']))} by battery-eta "
                  f"(models.json {ago(max(0.0, now - health['trained']))} old)")
    elif health.get('models_outdated'):
        fitted = "fitted here: models.json is from before an update (waiting for battery-eta's first refit)"
    elif age is not None:
        fitted = f'fitted here: models.json is {ago(age)} old (battery-eta not training?)'
    else:
        fitted = 'fitted here: no models.json from battery-eta'
    parts.append('battery model: ' + ', '.join(filter(None, [model, fitted])))
    if health.get('blocks'):
        span = health.get('levers_span')
        parts.append(f"settings model: {health['blocks']:,} experiment blocks" + (f' ({days(*span)})' if span else ''))
    if health.get('ready') is False:
        parts.insert(0, 'the store is still recomputing older minutes after an update (about a minute; '
                        'the panel reloads them when it is done)')
    return 'Data: ' + '; '.join(parts) + '.'


def what_if_note(result):
    """The what-if chart's caption: what it shows, what it was computed on, and
    the draw it starts from (your settings: experiments' settings taken out)."""
    info = result.get('info')
    if not info:
        return (f'Not enough battery time for model estimates: they need {MIN_ROWS} minutes on battery, and the '
                f"last {HISTORY_DAYS} days have {((result.get('health') or {}).get('battery') or 0):,}.")
    draw = ''
    if finite(info.get('power')):
        held = (f"; experiments' settings, {info['settings_w']:+.1f} W on average, taken out"
                if info.get('experiment_rows') and finite(info.get('settings_w')) else '')
        draw = f", at your settings ({info['power']:.1f} W{held})"
    return (f"Model estimates over {result.get('scope') or 'this period'}{draw}: the battery model removes an "
            "app's or group's activity, or dims the display, and assumes nothing else changes (outlined bars; "
            "whiskers are 90% intervals)."
            + (f" Models trained {time.strftime('%H:%M', time.localtime(result['trained']))} by battery-eta."
               if result.get('trained') else ' Models fitted here (battery-eta has not trained recently).'))


def typical_text(result):
    """The typical draw every gain is computed at, and what it is."""
    typical, energy = result.get('typical'), result.get('energy')
    if not finite(typical, energy) or typical <= 0:
        return ''
    return (f"Typical draw on battery {typical:.1f} W ({result.get('typical_note') or 'typical'}) → "
            f'{energy / typical:.1f} h per full charge ({energy:.0f} Wh usable); gains are at this draw. ')


def tally(quality, vote, name):
    return (((quality or {}).get(vote) or {}).get('tally') or {}).get(name)


def lever_rows(settings, levers, quality, pool, risky=(), quarantine=None):
    """What each setting's row in the Experiments tab shows: [{'name', 'label',
    'description', 'detail', 'effect', 'value', 'evidence', 'included',
    'sensitive', 'include_hint'}]."""
    quarantine, effects = quarantine or {}, {e['experiment']: e for e in levers.get('effects') or []}
    rows = []
    for spec in settings:
        name = spec['name']
        if spec.get('error'):
            detail = f"Unavailable: {spec['error']}"
        else:
            saving = spec.get('saving') is not None and spec.get('current') == spec.get('saving')
            other = (spec.get('normal') if saving else spec.get('saving')) or spec.get('alternative')
            detail = f"Now {spec.get('current')}{' (power-saving)' if saving else ''} · or {other}"
        down, up = tally(quality, 'down', name), tally(quality, 'up', name)
        detail += (f" · 👎 {down['on']['votes']}/{down['on']['windows']} blocks on" if down and down['on']['windows']
                   else f" · {spec.get('visible') or 'not noticeable'}")
        if up and ((quality or {}).get('up') or {}).get('votes'):
            detail += f" · 👍 {up['on']['votes']}/{up['on']['windows']} on, {up['off']['votes']}/{up['off']['windows']} off"
        if quality and report.qol_risk(quality, {name: True}) > report.QOL_RISK:
            detail += ' — kept off while you are here'
        includable = not spec.get('error') and spec.get('saving') is not None
        hint = 'Let automatic experiments vary this setting'
        if name in quarantine:
            why = quarantine[name]
            detail += f" · quarantined after a hardware incident{f' ({why})' if why else ''}"
            includable, hint = False, 'Quarantined after a hardware incident: automatic experiments leave it alone'
        elif name in risky and name in pool:  # In the pool only if added with --risky (power-experiment pool).
            detail += ' · risky: in automatic runs (added with --risky)'
            hint = ('Risky for the hardware (PCIe/NVMe/fabric crashes); you added it with --risky. Untick to take '
                    f'it out of automatic runs (back in only with `power-experiment pool add {name} --risky`)')
        elif name in risky:
            detail += ' · risky: not in automatic runs'
            includable, hint = False, ('Risky for the hardware (PCIe/NVMe/fabric crashes): automatic experiments '
                                       f'vary it only after `power-experiment pool add {name} --risky`')
        effect, progress = effects.get(name), (levers.get('progress') or {}).get(name)
        if effect and finite(effect.get('chance')):
            detail += f" · {effect['chance']:.0%} chance it saves"
        if effect:
            blocks = effect.get('blocks') or {}
            evidence = f"{blocks.get('on', 0)}+{blocks.get('off', 0)} blocks, {effect.get('hours', 0):.1f} h"
            value = gain_text(effect) or f"{effect.get('watts', 0):+.2f} W"
        elif progress:
            evidence, value = f"{progress['on']}+{progress['off']} blocks so far", 'needs more'
        else:
            evidence = 'CPU setting: needs ~4× the time' if name in report.CPU_LEVERS else ''
            value = 'not measured'
        rows.append({'name': name, 'label': spec.get('label', name), 'description': spec.get('description', ''),
                     'detail': detail, 'effect': effect, 'value': value, 'evidence': evidence,
                     'included': includable and name in pool, 'sensitive': includable, 'include_hint': hint})
    return rows


GONE = ''  # picker's `chosen` once the chosen app exited and nothing else was chosen since.


def picker(units, shown, chosen):
    """The "Close an app" picker's entries: the PICKER apps with the most CPU time,
    plus the chosen one wherever it ranks. `chosen` is a unit, None before any
    choice (the first app is chosen) or GONE. None while the same apps are listed
    (the list isn't replaced under an open dropdown as CPU times grow); else
    (units, labels, index). When the chosen app exited, the first entry is a
    placeholder (unit None) and stays chosen until another app is: never
    another app at the same position (a dropdown can't choose nothing)."""
    listed = units[:PICKER] + [u for u in units[PICKER:] if u['unit'] == chosen]
    names = [u['unit'] for u in listed]
    labels = [f"{u['label']}  ({u['cpu_s'] // 60} CPU-min)" for u in listed]
    if chosen is not None and chosen not in names:
        names, labels = [None] + names, ['The chosen app exited: choose one'] + labels
    if set(names) == set(shown or ()):
        return None
    return names, labels, names.index(chosen) if chosen in names else 0


def notices(err):
    """power-experiment's own notices on stderr ('power-experiment: …' lines, prefix
    dropped): what a command that succeeded left undone."""
    return [line.split(':', 1)[1].strip() for line in (err or '').splitlines()
            if line.startswith('power-experiment:')]


def refused(err):
    """{name: why} `power-experiment apply` (or `goal --apply`) refused: risky
    without --risky, quarantined (its 'not applied: a (why); b (why)' line)."""
    found = {}
    for notice in notices(err):
        if notice.startswith('not applied:'):
            for part in notice[len('not applied:'):].split(';'):
                name, _, why = part.strip().partition(' (')
                if name:
                    found[name] = why[:-1] if why.endswith(')') else why
    return found


def apply_note(assignments, out, err):
    """The note after `power-experiment apply <assignments>` (or `goal --apply`,
    whose combination may be unknown here: None), and for how long (None: until
    the next command). Settings it refused are never called applied."""
    names = None if assignments is None else list(dict.fromkeys(a.partition('=')[0] for a in assignments))
    left, rest = refused(err), [n for n in notices(err) if not n.startswith('not applied:')]
    if names == [] and not left and not rest:
        return 'Nothing measured to apply yet.', None
    applied = None if names is None else [name for name in names if name not in left]
    when = ' (it takes effect on battery)' if 'On AC' in (out or '') else ''
    parts = []
    if applied is None:
        parts.append(f'Applied until reboot{when}.')
    elif applied:
        parts.append(f"Applied until reboot{when}: {', '.join(applied)}.")
    if left:
        parts.append(('Nothing applied' if applied == [] else 'Not applied') + ': '
                     + '; '.join(f'{name} ({why})' if why else name for name, why in left.items()) + '.')
    parts += [f'{n}.' for n in rest]
    return ' '.join(parts), NOTE_SECONDS if left or rest else None


def command_note(ok, out, err, done=None):
    """What a finished command leaves under the Experiments banner, and for how
    long (None: until the next command). A failure's error, or power-experiment's
    notices from a success, stay NOTE_SECONDS: the 2 s refresh must not wipe
    them before they are read. `done` is a note, or a function of (stdout, stderr)
    returning (note, seconds)."""
    if not ok:
        return f"Command failed: {(err or '').strip()[:300] or 'no error output'}", NOTE_SECONDS
    if callable(done):
        return done(out or '', err or '')
    extra = notices(err)
    if extra:
        return ' '.join(filter(None, [done] + [f'{n}.' for n in extra]))[:400], NOTE_SECONDS
    return done or '', None


def interaction_clear(interaction):
    """Whether a pair's extra saving is clear: lever_effects' `clear` (a
    prespecified pair at p < 0.1, any other at a 10% false discovery rate over
    all pairs: with 45 pairs, ~4.5 intervals would exclude zero by chance)."""
    return interaction.get('clear') is True


def read_estimate(path, now=None):
    """battery-eta's latest estimate (eta.json) while under 15 s old, else None."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not finite(data.get('updated')) or (now or time.time()) - data['updated'] >= 15:
        return None
    return data


def read_battery(root=Path('/sys/class/power_supply')):
    """The battery's own readings ({'pct', 'status', 'watts', 'charge_wh'});
    one that can't be read is left out."""
    battery = next(iter(sorted(Path(root).glob('BAT*'))), None)
    if battery is None:
        return {}

    def number(name):
        try:
            return int((battery / name).read_text())
        except (OSError, ValueError):
            return None
    out = {}
    try:
        out['status'] = (battery / 'status').read_text().strip()
    except OSError:
        pass
    pct, current, volts, charge = (number(n) for n in ('capacity', 'current_now', 'voltage_now', 'charge_now'))
    if pct is not None:
        out['pct'] = pct
    if current is not None and volts is not None:
        out['watts'] = current * volts / 1e12
    if charge is not None and volts is not None:
        out['charge_wh'] = charge * volts / 1e12
    return out


def status_line(estimate, battery, now=None):
    """The panel's header: battery-eta's estimate (eta.json, None when stale),
    else the battery's own readings. The numbers follow the estimate's own
    status: sysfs can say Charging a moment before eta.json does, and mixing
    the two (a charging line from a discharging estimate) once raised a
    KeyError that stopped the panel's refresh timer. Time left is awake time
    on battery; the second line is how long a suspend from now would last."""
    now, battery = now or time.time(), battery or {}
    pct, line = battery.get('pct'), ''
    if estimate and finite(estimate.get('hours')):
        # battery-eta's own kind (its classes): sysfs's Unknown on battery is discharging to it.
        classes = estimate.get('classes') if isinstance(estimate.get('classes'), list) else ()
        status = ('Discharging' if 'discharging' in classes else 'Charging' if 'charging' in classes
                  else str(estimate.get('status') or battery.get('status') or 'unknown'))
        pct = pct if pct is not None else estimate.get('pct')
        watts = estimate.get('watts') if finite(estimate.get('watts')) else battery.get('watts')
        parts = [(f'{pct:.0f}% charge' if finite(pct) else 'Battery')
                 + (f", {estimate['energy_pct']:.0f}% energy" if finite(estimate.get('energy_pct')) else ''),
                 status.lower()] + ([f'{abs(watts):.1f} W'] if finite(watts) else [])
        low, high = estimate.get('low'), estimate.get('high')
        interval = f' (80%: {duration(low)}–{duration(high)})' if finite(low, high) else ''
        if status == 'Discharging' and finite(estimate.get('cutout')):
            clock = lambda t: time.strftime('%H:%M:%S', time.localtime(t))
            poweroff = estimate.get('poweroff') if finite(estimate.get('poweroff')) else None
            end = poweroff or estimate['cutout']
            pm = f", ±{estimate['cutout_pm']:.0f} s" if finite(estimate.get('cutout_pm')) else ''
            parts.append(f"{'powers off' if poweroff else 'cuts out'} at {clock(end)} "
                         f"(in {eta.countdown(end - now)}{pm}"
                         + (f"; the battery cuts out at {clock(estimate['cutout'])})" if poweroff else ')'))
        elif status == 'Discharging':
            parts.append(f"~{duration(estimate['hours'])} left awake{interval}")
        elif status == 'Charging':
            if finite(estimate.get('ratio')):
                parts.append(f"×{estimate['ratio']:.1f} minutes of use per minute charging"
                             + (f" (80%: ×{estimate['ratio_low']:.1f}–{estimate['ratio_high']:.1f})"
                                if finite(estimate.get('ratio_low'), estimate.get('ratio_high')) else ''))
            target = estimate.get('target')
            parts.append(f"{duration(estimate['hours'])} to {f'{target:.0f}%' if finite(target) else 'the limit'}"
                         f'{interval}')
        line = ' · '.join(parts)
    elif battery.get('status'):
        status, watts = battery['status'], battery.get('watts')
        line = ' · '.join(filter(None, [f'{pct}%' if pct is not None else '', status.lower()]))
        if status == 'Discharging' and finite(watts, battery.get('charge_wh')) and watts > 0.5:
            line += f" · {watts:.1f} W · ~{duration(battery['charge_wh'] / watts)} left awake at this draw"
    asleep = (estimate or {}).get('sleep')
    if isinstance(asleep, dict) and finite(asleep.get('hours')):
        line += (('\n' if line else '') + f"Asleep from now: ~{long_duration(asleep['hours'])}"
                 + (f" (80%: {long_duration(asleep['low'])}–{long_duration(asleep['high'])})"
                    if finite(asleep.get('low'), asleep.get('high')) else '')
                 + (f" at {asleep['watts']:.1f} W" if finite(asleep.get('watts')) else ''))
    return line


def banner_text(state, enabled, now=None, frozen='', quarantine=None):
    """The Experiments tab's banner: what the runner is doing (experiment.json)."""
    now, state = now or time.time(), state or {}
    status, auto = state.get('status'), state.get('mode') == 'auto'
    settling = 'Settling (first minute ignored).' if now < (state.get('washout_until') or 0) else ''
    config = state.get('config') if isinstance(state.get('config'), dict) else {}
    if auto and status == 'running':
        arms = ', '.join(PHRASES.get(n, (n, n))[0 if arm == 'B' else 1] for n, arm in config.items())
        plan = state.get('plan') or {}
        allowed = (f" ({plan['allowed']} of {plan['combinations']} combinations allowed)"
                   if plan.get('combinations') else '')
        text = f"Block {state.get('block', '?')}: {arms}{allowed}. {settling}"
    elif auto and status == 'paused':
        text = f"Paused: {state.get('reason') or 'no reason given'}; your settings are restored meanwhile."
    elif status == 'running':
        names = state.get('names') or [state.get('name')]
        arms = ', '.join(f"{n} {config.get(n, '?')}" for n in names) if config else state.get('arm', '?')
        progress = (f"{duration(state.get('collected', 0) / 3600)} of {duration(state['target_seconds'] / 3600)} "
                    'on battery collected. ' if finite(state.get('target_seconds')) else '')
        text = (f"Running: {state.get('label', 'an experiment')} — block {state.get('block', '?')}: {arms} "
                '(A = as before). ' + progress + settling + frozen)
    elif status == 'paused':
        reason = state.get('reason') or 'no reason given'
        if reason == 'waiting for the screen to lock':
            reason += ' (apps keep running until you lock with Ctrl+Alt+L)'
        text = f"Paused: {state.get('label', 'an experiment')} — {reason}. Settings restored meanwhile." + frozen
    elif enabled:
        text = 'On; starting…'
    else:
        text = 'Off. Turn on to let the laptop test settings by itself while on battery.'
    if quarantine:
        text += ('\nQuarantined after a hardware incident (experiments leave them alone): '
                 + ', '.join(f"{n}{f' ({why})' if why else ''}" for n, why in quarantine.items()) + '.')
    return text.strip()


def check(out=sys.stdout, budget=BUDGET, history=None):
    """Gather each period the panel shows (from the real database by default);
    0 if every part worked within the budget."""
    history, failed, result = history or History(), False, None
    for name, length in PERIODS.items():
        result = gather(time.time() - length, time.time(), history)
        slow = sorted(result['timings'].items(), key=lambda item: -item[1])[:3]
        print(f"{name:5} {result.get('seconds', 0):5.2f} s  slowest: "
              + ', '.join(f'{part} {seconds:.2f} s' for part, seconds in slow), file=out)
        for part, error in result['errors'].items():
            print(f'      {part} FAILED: {error}', file=out)
        if result['errors'] or result.get('seconds', budget + 1) > budget:
            failed = True
    if result and 'health' in result:
        print(health_text(result['health']), file=out)
    print('FAILED' if failed else 'ok', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(check())
