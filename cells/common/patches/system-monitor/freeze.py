#!/usr/bin/env python3
"""Cached-app freeze, after Android's: on battery, an app you chose that hasn't
had focus for AFTER and has no window on a workspace in view is frozen with the
cgroup freezer, so it uses no CPU at all. It thaws the moment you focus it or
switch to its workspace (niri's events), when you start it again (a new scope
with its name: a second instance hands its arguments to the running one and
would wait for it), and on AC. Every EVERY it thaws for THAW, so timers, sync
and connections catch up. An app busier than BUSY of a core over the last
BUSY_WINDOW (a build, an export) is left running, and one playing or recording
audio or video counts as in use. Its last focus is niri's focus_timestamp, so
a restart doesn't reset it.

Android can freeze every cached app because its apps are written to be frozen;
desktop apps aren't. So only the apps you list are frozen: not chat apps (their
connection drops while frozen), and nothing that serves other programs (a dev
server stops answering).

    app-freeze PATTERN ... [--after MIN] [--every MIN] [--thaw SEC]
    app-freeze --release      (thaw what it froze; the unit's ExecStopPost)

PATTERN is matched (case-insensitively) anywhere in the app's scope names in
app.slice, systemd's escapes undone, as app-throttle's --keep is. It follows
focus and media from the same event streams as app-throttle (throttle.Session).
RUNTIME/freeze.json says what it watches and what it has frozen.
"""
import argparse
import collections
import json
import signal
import sys
import time
from pathlib import Path

import throttle
from throttle import APPS, RUNTIME, Session, app_of, label, on_battery, read, unescape

AFTER = 30 * 60  # s without focus before an app is frozen.
EVERY = 10 * 60  # s between thaws while frozen.
THAW = 10  # s each thaw lasts.
BUSY, BUSY_WINDOW = 0.25, 5 * 60  # Cores, over s: busier apps are working, not idling.
STATE = RUNTIME / 'freeze.json'


class Freezer(Session):
    state_path = STATE

    def __init__(self, patterns, apps=APPS, battery=on_battery, proc=Path('/proc'), clock=time.monotonic,
                 after=AFTER, every=EVERY, thaw=THAW, busy=BUSY):
        super().__init__(apps, battery, proc, clock)
        self.patterns = tuple(p.lower() for p in patterns)
        self.after, self.every, self.thaw, self.busy = after, every, thaw, busy
        # By app: last in use; when frozen (this time); when a periodic thaw began; (time, CPU µs) samples.
        self.used, self.frozen, self.thawed, self.usage, self.was_busy = {}, {}, {}, {}, {}
        self.scopes = None  # The scopes seen last time (None before the first look).
        self.mine = set()  # Scopes this froze (a power-experiment freeze is left alone).

    def chosen(self, scope):
        name = unescape(scope).lower()
        return any(pattern in name for pattern in self.patterns)

    def cpu_seconds(self, scopes):
        total = 0.0
        for scope in scopes:
            for line in (read(self.apps / scope / 'cpu.stat') or '').splitlines():
                key, _, value = line.partition(' ')
                if key == 'usage_usec':
                    total += int(value) / 1e6
        return total

    def busy_now(self, app, scopes, now):
        """Whether the app used more than BUSY of a core over the last BUSY_WINDOW."""
        history = self.usage.setdefault(app, collections.deque())
        if history and now - history[-1][0] < 1:  # Event bursts: one sample a second is plenty.
            return self.was_busy.get(app, False)
        history.append((now, self.cpu_seconds(scopes)))
        while len(history) > 2 and now - history[1][0] >= BUSY_WINDOW:
            history.popleft()
        (t0, used0), (t1, used1) = history[0], history[-1]
        self.was_busy[app] = t1 - t0 >= 60 and (used1 - used0) / (t1 - t0) > self.busy
        return self.was_busy[app]

    def wanted(self):
        """{scope: freeze?} for the scopes of the apps chosen."""
        now, battery = self.clock(), self.battery()
        scopes = sorted(p.name for p in self.apps.glob('app-*.scope'))
        new = set(scopes) - self.scopes if self.scopes is not None else set()
        self.scopes = set(scopes)
        apps = collections.defaultdict(list)
        for scope in scopes:
            apps[app_of(scope)].append(scope)
        chosen = {app: own for app, own in apps.items() if any(self.chosen(s) for s in own)}
        in_use = self.playing() | self.shown() | {self.focused_app()}
        focused = self.last_focused()
        values = {}
        for app, own in chosen.items():
            names = {label(s).lower() for s in own}
            relaunched = any(app_of(s) != app and label(s).lower() in names for s in new)
            if app in in_use or relaunched:
                self.used[app] = now
            elif app not in self.used:  # First seen: when it last had focus, if niri knows.
                self.used[app] = min(now, focused.get(app, now))
            busy = self.busy_now(app, own, now)
            if not battery or busy or now - self.used[app] < self.after:
                self.frozen.pop(app, None)
                self.thawed.pop(app, None)
                freeze = False
            elif app in self.thawed:  # A periodic thaw: over after THAW.
                freeze = now - self.thawed[app] >= self.thaw
                if freeze:
                    del self.thawed[app]
                    self.frozen[app] = now
            elif app in self.frozen and now - self.frozen[app] >= self.every:
                del self.frozen[app]
                self.thawed[app] = now
                freeze = False
            else:
                self.frozen.setdefault(app, now)
                freeze = True
            values.update(dict.fromkeys(own, freeze))
        for gone in set(self.used) - set(chosen):
            for state in (self.used, self.frozen, self.thawed, self.usage, self.was_busy):
                state.pop(gone, None)
        return values

    def apply(self, values=None):
        """Freeze and thaw what changed; the scopes now frozen by this."""
        values = self.wanted() if values is None else values
        for scope, freeze in values.items():
            path = self.apps / scope / 'cgroup.freeze'
            current = read(path)
            try:
                if freeze and current == '0':
                    path.write_text('1')
                    self.mine.add(scope)
                elif not freeze and scope in self.mine:
                    if current == '1':
                        path.write_text('0')
                    self.mine.discard(scope)
            except OSError:
                self.mine.discard(scope)  # The app exited meanwhile.
        self.mine &= {p.name for p in self.apps.glob('app-*.scope')}
        return sorted(self.mine)

    def release(self):
        self.apply(dict.fromkeys(self.mine, False))

    def step(self, media_known):
        """Apply what is wanted now; the state file's contents. Without stream
        data (PipeWire down) nothing stays frozen: an app might be wanted for media."""
        frozen = self.apply() if media_known else self.apply(dict.fromkeys(self.wanted(), False))
        return {'on_battery': self.battery(), 'scopes': frozen,  # An app's scopes can differ in case: one name.
                'frozen': sorted({label(s).lower() for s in frozen}),
                'watching': sorted({label(s).lower() for s in self.scopes or () if self.chosen(s)})}


def release_left(state=STATE, apps=APPS):
    """Thaw the scopes a previous run's state file says it froze (after a crash)."""
    try:
        scopes = json.loads(Path(state).read_text()).get('scopes') or []
    except (OSError, ValueError, AttributeError):
        scopes = []
    for scope in scopes:
        try:
            if read(apps / scope / 'cgroup.freeze') == '1':
                (apps / scope / 'cgroup.freeze').write_text('0')
        except OSError:
            pass
    Path(state).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='app-freeze', description=__doc__.split('\n\n')[0])
    parser.add_argument('patterns', nargs='*', metavar='PATTERN', help='apps to freeze (matched in scope names)')
    parser.add_argument('--after', type=float, default=AFTER / 60, help='minutes without focus before freezing')
    parser.add_argument('--every', type=float, default=EVERY / 60, help='minutes between thaws while frozen')
    parser.add_argument('--thaw', type=float, default=THAW, help='seconds each thaw lasts')
    parser.add_argument('--release', action='store_true', help='thaw what a previous run froze and exit')
    args = parser.parse_args(argv)
    if args.release:
        release_left()
        return
    if not args.patterns:
        parser.error('name at least one app to freeze')
    release_left()  # A crashed run's freezes: start from thawed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    throttle.run(Freezer(args.patterns, after=args.after * 60, every=args.every * 60, thaw=args.thaw))


if __name__ == '__main__':
    main()
