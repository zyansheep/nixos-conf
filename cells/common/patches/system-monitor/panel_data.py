#!/usr/bin/env python3
"""The battery panel's data: everything it shows for one period, computed off
the UI thread. GTK-free, so test_panel_data.py runs it headless on a synthetic
month of every kind of data, and `battery-panel-check` runs it on the real
power database.

    battery-panel-check     (each period the panel shows: time per part, and errors)

Each part is computed on its own: one that fails falls back to an empty value,
its error goes in `errors` (with the traceback on stderr, so in the journal),
and the rest still shows. A failure in one analysis once left the whole panel
blank.
"""
import sys
import threading
import time
import traceback

import eta  # First: it limits BLAS threads before numpy loads.
import experiment
import report

FREEZES = ('freeze', 'freeze-apps')  # Paired A/B runs, analysed apart from the setting model.
HISTORY_DAYS = 30
BUDGET = 5.0  # s: battery-panel-check fails a period that takes longer to gather.
PERIODS = {'6 h': 6 * 3600, 'Day': 86400, 'Week': 7 * 86400}


class History:
    """The last HISTORY_DAYS of minutes and events. While the panel is open, a
    reload (range switch, paging, the minute tick) fetches only minutes from the
    newest one on, which may still have been filling."""

    def __init__(self, load=None, clock=time.time):
        self.load, self.clock = load or report.load_range, clock
        self.lock = threading.Lock()
        self.minutes, self.events, self.since = [], [], None
        self.version, self.cache = None, {}

    def get(self):
        with self.lock:
            now = self.clock()
            start = now - HISTORY_DAYS * 86400
            if self.since is None:
                self.minutes, self.events = self.load(start, now + 60)
            else:
                minutes, events = self.load(self.since, now + 60)
                known = {(e.get('event'), e.get('t1')) for e in self.events}
                self.minutes = [m for m in self.minutes if start <= m['t'] < self.since] + minutes
                self.events = ([e for e in self.events if e.get('t1', 0) >= start]
                               + [e for e in events if (e.get('event'), e.get('t1')) not in known])
            self.since = self.minutes[-1]['t'] if self.minutes else None
            self.version = (len(self.minutes), self.since, len(self.events))
            return list(self.minutes), list(self.events)

    def memo(self, name, key, compute):
        """`compute()`, reused while the history and `key` are unchanged: paging
        and switching periods don't refit the history-wide models."""
        key = (self.version, key)
        if name in self.cache and self.cache[name][0] == key:
            return self.cache[name][1]
        value = compute()
        self.cache[name] = (key, value)
        return value


def no_levers():
    """lever_effects' result before any setting has been measured."""
    return {'effects': [], 'interactions': [], 'blocks': 0, 'current': {}, 'coef': {}, 'samples': {},
            'sigma': None, 'sigma_raw': None, 'progress': {}}


def gather(start, end, history=None, on_period=None):
    """Everything the panel shows for [start, end). Every key is always there;
    `errors` maps each part that failed to its error, `timings` each part to
    seconds. Only an unreadable database gives `error` alone.

    The period's own parts (timeline, sleep, apps and display) come first and,
    if given, go to `on_period` (with `pending` set) while the history-wide
    ones — the experiments, fitted on all HISTORY_DAYS, with gains at the
    typical draw over them — are computed, or reused from `history` when it
    hasn't changed."""
    errors, timings = {}, {}
    began = time.perf_counter()
    history = history or History()

    def part(name, compute, fallback):
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

    loaded = part('history', history.get, None)
    if loaded is None:
        return {'error': f"Couldn't read the power database: {errors['history']}", 'errors': errors,
                'timings': timings}
    minutes_all, events = loaded
    rows_all = report.battery_rows(minutes_all)
    # battery-eta trains every model; fit here only if its models.json is missing or stale.
    shared = part('models', eta.load_models, None)
    model = part('battery model', lambda: shared['attribution'] if shared else report.fit_rows(rows_all), None)
    minutes = [m for m in minutes_all if start <= m['t'] < end]
    if start < time.time() - HISTORY_DAYS * 86400:
        older = part('older history', lambda: report.load_range(start, end), None)
        if older is not None:
            minutes, events = older[0], older[1] + events
    period_events = [e for e in events if start - 86400 <= e.get('t1', 0) < end + 86400]
    rows = report.battery_rows(minutes)
    scope = 'this period'
    if len(rows) < 30:
        rows, scope = rows_all, 'all history (too little battery time in this period)'
    energy = part('energy', lambda: report.full_energy(minutes_all), None)
    estimates, info = part('apps and display', lambda: report.what_if(rows, energy, model=model, train_rows=rows_all)
                           if rows and model else ([], None), ([], None))
    typical = sum(m['bat'] for m in rows_all) / len(rows_all) if rows_all else None
    result = dict(points=part('timeline', lambda: report.timeline(minutes, model) if model else [], []),
                  span=(start, end), sleeps=part('sleep drain', lambda: [s for s in report.sleep_drain(period_events)
                                                                         if s['end'] > start and s['start'] < end], []),
                  sleeps_all=part('sleep history', lambda: report.sleep_drain(events), []), estimates=estimates,
                  info=info, scope=scope, energy=energy, power=info['power'] if info else typical, typical=typical,
                  model=model, trained=shared['fitted'] if shared else None, errors=errors, timings=timings)
    if on_period:
        on_period(dict(result, pending=True, errors=dict(errors)))
    pool = part('experiment pool', experiment.pool, [])
    cached = lambda name, compute, *key: history.memo(name, (energy, typical, *key), compute)
    levers = part('settings model', lambda: cached('levers', lambda: report.lever_effects(minutes_all, energy, typical)),
                  no_levers())
    # App freezing runs only while locked, so it stays a separate paired A/B analysis.
    freezes = part('app freezing', lambda: cached('freezes', lambda: [
        e for e in report.experiment_effects(minutes_all, energy, typical) if e['experiment'] in FREEZES]
        if typical else []), [])
    result.update(levers=levers, effects=freezes, pool=pool,
                  plan=part('experiment plan', lambda: cached('plan', lambda: report.plan(minutes_all, events, pool)
                                                              if pool else None, tuple(pool)), None),
                  goal=part('goal', experiment.goal, 'battery'),
                  psr=part('panel self-refresh', lambda: cached('psr', lambda: report.boot_effect(minutes_all, energy,
                                                                                                   typical)), None),
                  psr_arm=part('boot arm', experiment.psr_arm, None),
                  catalog=part('settings', lambda: [experiment.describe(spec)
                                                    for spec in experiment.catalog().values()], []),
                  units=part('app scopes', lambda: experiment.freezable_units()[:12], []),
                  seconds=round(time.perf_counter() - began, 3))
    return result


def check(out=sys.stdout, budget=BUDGET, history=None):
    """Gather each period the panel shows (from the real database by default);
    0 if every part worked within the budget."""
    history, failed = history or History(), False
    for name, length in PERIODS.items():
        result = gather(time.time() - length, time.time(), history)
        slow = sorted(result['timings'].items(), key=lambda item: -item[1])[:3]
        print(f"{name:5} {result.get('seconds', 0):5.2f} s  slowest: "
              + ', '.join(f'{part} {seconds:.2f} s' for part, seconds in slow), file=out)
        for part, error in result['errors'].items():
            print(f'      {part} FAILED: {error}', file=out)
        if result['errors'] or result.get('seconds', budget + 1) > budget:
            failed = True
    print('FAILED' if failed else 'ok', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(check())
