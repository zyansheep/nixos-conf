#!/usr/bin/env python3
"""Background-app throttle: while on battery, every app with a window but the
focused one runs under a CPU quota of QUOTA% of one core (cgroup cpu.max),
lifted the moment it gets focus and kept off for GRACE after it loses it, so
switching back and forth never lands on a cold app. Apps without a window —
helpers, daemons, clipboard owners (wl-copy serves every paste), whatever a
launcher just spawned while a layer surface had focus — are never capped:
capped, the emoji picker took 4–6 s to paste. Apps playing or recording audio or video
(PipeWire streams that are running, not just open) and KEEP are left alone,
and so is everything on AC. KEEP is where real work runs in the background:
terminals and editors/IDEs (builds, agent CLIs and language servers run in
their scopes; capped, a build ran 10× slower whenever another window had
focus), T3 Code's agents and the input method that serves the focused app.

An app is every systemd scope in app.slice with the same launch pid: Electron
apps move their main process into a scope of their own (app-vesktop-2295)
and leave the renderers in the one niri started (app-niri-vesktop-2295), so
freeing only the window's scope left Discord's renderers capped — frozen —
while it was focused. The quota is the app's: its scopes share it equally (a
two-scope app gets QUOTA/2 per scope, not QUOTA each).

    app-throttle [--quota N] [--keep PATTERN ...]
    app-throttle --release      (lift every quota; the unit's ExecStopPost)

app-throttle.service runs it; power-experiment's throttle-apps setting starts
and stops that unit. It follows focus from niri's event stream and streams
from `pw-dump --monitor`, so between changes it does nothing but a RESCAN for
new apps and AC changes. RUNTIME/throttle.json says what it throttles, for
waybar-monitor. Stopping it lifts every quota.
"""
import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

QUOTA = 10  # % of one core per background app, shared by its scopes.
GRACE = 120  # s an app stays free after it loses focus.
PERIOD = 100_000  # µs: cgroup cpu.max's period.
MINIMUM = 1000  # µs: the smallest quota cpu.max takes.
UNLIMITED = f'max {PERIOD}'
# Matched (case-insensitively) anywhere in the scope's name, systemd escapes undone.
TERMINALS = ('foot', 'kitty', 'alacritty', 'wezterm', 'ghostty', 'konsole', 'gnome-terminal', 'xterm',
             'terminator', 'tilix')
EDITORS = ('code', 'codium', 'zed', 'emacs', 'neovide', 'jetbrains', 'sublime', 'gvim')  # 'code': VS Code, T3 Code.
# Agents keep working; the input method types into the focused app; builds run in terminals and editors.
KEEP = ('t3code', 'fcitx5') + TERMINALS + EDITORS
RESCAN = 5  # s: new apps, AC changes.
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'
STATE = RUNTIME / 'throttle.json'
APPS = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice')
MEDIA = ('Stream/Output/Audio', 'Stream/Input/Audio', 'Stream/Output/Video', 'Stream/Input/Video')


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def on_battery(power=Path('/sys/class/power_supply')):
    return not any(read(supply / 'type') == 'Mains' and read(supply / 'online') == '1' for supply in power.glob('*'))


class Session:
    """What the desktop is doing, from niri's and PipeWire's event streams:
    which app has focus, which have windows, which play or record media.
    app-throttle and app-freeze (freeze.py) act on it."""

    def __init__(self, apps=APPS, battery=on_battery, proc=Path('/proc'), clock=time.monotonic):
        self.apps, self.battery, self.proc, self.clock = apps, battery, proc, clock
        self.windows, self.focused, self.nodes = {}, None, {}  # window id → pid; PipeWire node id → what we know
        # Window id → its workspace, and when it last had focus (niri's focus_timestamp: CLOCK_MONOTONIC, s).
        self.placed, self.focus_times = {}, {}
        self.workspaces = {}  # Workspace id → [output, active]

    def window(self, window):
        self.windows[window['id']] = window.get('pid')
        self.placed[window['id']] = window.get('workspace_id')
        if stamp := window.get('focus_timestamp'):
            self.focus_times[window['id']] = stamp.get('secs', 0) + stamp.get('nanos', 0) / 1e9

    def window_event(self, event):
        """One line of `niri msg -j event-stream`."""
        if 'WindowsChanged' in event:
            windows = event['WindowsChanged']['windows']
            self.windows, self.placed, self.focus_times = {}, {}, {}
            for window in windows:
                self.window(window)
            self.focused = next((w['id'] for w in windows if w.get('is_focused')), None)
        elif 'WindowOpenedOrChanged' in event:
            window = event['WindowOpenedOrChanged']['window']
            self.window(window)
            if window.get('is_focused'):
                self.focused = window['id']
        elif 'WindowClosed' in event:
            for known in (self.windows, self.placed, self.focus_times):
                known.pop(event['WindowClosed']['id'], None)
        elif 'WindowFocusChanged' in event:
            self.focused = event['WindowFocusChanged']['id']
        elif 'WorkspacesChanged' in event:
            self.workspaces = {w['id']: [w.get('output'), bool(w.get('is_active'))]
                               for w in event['WorkspacesChanged']['workspaces']}
        elif 'WorkspaceActivated' in event:  # Active on its output, instead of the one there was.
            output = self.workspaces.get(event['WorkspaceActivated']['id'], [None])[0]
            for workspace, known in self.workspaces.items():
                if known[0] == output:
                    known[1] = workspace == event['WorkspaceActivated']['id']

    def media_event(self, objects):
        """One batch of `pw-dump --monitor`: nodes added, changed (possibly only
        what changed) or removed (info null)."""
        for node in objects:
            if node.get('type', 'PipeWire:Interface:Node') != 'PipeWire:Interface:Node' or 'id' not in node:
                continue
            info = node.get('info', {})
            if info is None:
                self.nodes.pop(node['id'], None)
                continue
            known = self.nodes.setdefault(node['id'], {})
            if (props := info.get('props')) is not None:
                known.update(media=props.get('media.class') in MEDIA, pid=props.get('application.process.id'))
            if 'state' in info:
                known['state'] = info['state']

    @property
    def media(self):
        """{node: pid} of audio/video streams that are running."""
        return {n: int(k['pid']) for n, k in self.nodes.items()
                if k.get('media') and k.get('pid') and k.get('state') == 'running'}

    def scope_of(self, pid):
        """The app scope `pid` runs in (a name in app.slice), or None."""
        if not pid:
            return None
        for line in (read(self.proc / str(pid) / 'cgroup') or '').splitlines():
            parts = line.rpartition(':')[2].split('/')
            if 'app.slice' in parts[:-1]:
                name = parts[parts.index('app.slice') + 1]
                return name if name.endswith('.scope') else None
        return None

    def focused_app(self):
        """The focused window's app (app_of), or None."""
        return app_of(self.scope_of(self.windows.get(self.focused)) or '') or None

    def playing(self):
        """Apps with an audio or video stream running."""
        return {app_of(self.scope_of(pid) or '') for pid in self.media.values()}

    def shown(self):
        """Apps with a window on a workspace that is active on its output: maybe
        on screen (niri doesn't say which of its columns are in view)."""
        return {app_of(self.scope_of(pid) or '') for window, pid in self.windows.items()
                if self.workspaces.get(self.placed.get(window), [None, False])[1]}

    def last_focused(self):
        """{app: when one of its windows last had focus} (niri's clock, CLOCK_MONOTONIC)."""
        out = {}
        for window, stamp in self.focus_times.items():
            app = app_of(self.scope_of(self.windows.get(window)) or '')
            if app:
                out[app] = max(out.get(app, stamp), stamp)
        return out


class Throttle(Session):
    state_path = STATE

    def __init__(self, apps=APPS, quota=QUOTA, keep=KEEP, battery=on_battery, proc=Path('/proc'), grace=GRACE,
                 clock=time.monotonic):
        super().__init__(apps, battery, proc, clock)
        self.keep, self.grace = tuple(k.lower() for k in keep), grace
        self.quota = quota * PERIOD // 100  # µs per period for a whole app.
        self.limit = f'{self.quota} {PERIOD}'
        self.current, self.left = None, {}  # The focused app; when each app last lost focus.

    def wanted(self):
        """{scope: cpu.max} for every app scope."""
        scopes = sorted(p.name for p in self.apps.glob('app-*.scope'))
        if not self.battery():
            return dict.fromkeys(scopes, UNLIMITED)
        now = self.clock()
        focused = self.focused_app()
        if focused != self.current:
            if self.current is not None:
                self.left[self.current] = now
            self.current = focused
        free = self.playing() | {focused}
        self.left = {app: left for app, left in self.left.items() if now - left < self.grace}
        free |= set(self.left)
        free |= {app_of(s) for s in scopes if any(k in unescape(s).lower() for k in self.keep)}
        windowed = {app_of(self.scope_of(pid) or '') for pid in self.windows.values()} - {''}
        capped = {s for s in scopes if app_of(s) in windowed and app_of(s) not in free}
        shares = {}  # The app's quota, split between its scopes.
        for scope in capped:
            shares[app_of(scope)] = shares.get(app_of(scope), 0) + 1
        return {s: self.share(shares[app_of(s)]) if s in capped else UNLIMITED for s in scopes}

    def share(self, scopes):
        return f'{max(MINIMUM, self.quota // scopes)} {PERIOD}'

    def apply(self, values=None):
        """Write what changed; the scopes now throttled."""
        values = self.wanted() if values is None else values
        for scope, value in values.items():
            path = self.apps / scope / 'cpu.max'
            if read(path) != value:
                try:
                    path.write_text(value)
                except OSError:
                    pass  # The app exited meanwhile.
        return sorted(s for s, v in values.items() if v != UNLIMITED)

    def release(self):
        self.apply(dict.fromkeys((p.name for p in self.apps.glob('app-*.scope')), UNLIMITED))

    def step(self, media_known):
        """Apply what is wanted now; the state file's contents. Without stream
        data (PipeWire down), apps that might be playing stay unthrottled."""
        throttled = self.apply() if media_known else self.apply(dict.fromkeys(self.wanted(), UNLIMITED))
        return {'quota': self.limit, 'on_battery': self.battery(),
                'throttled': sorted({label(s) for s in throttled}, key=str.lower)}


def app_of(scope):
    """An app's scopes share its launch pid: 'app-niri-vesktop-2295.scope' and
    'app-vesktop-2295.scope' are both '2295'. A scope without one is its own app."""
    match = re.search(r'-(\d+)\.scope$', scope)
    return match[1] if match else scope


def unescape(scope):
    """systemd's escapes: 'emoji\\x2dpicker' → 'emoji-picker'."""
    return re.sub(r'\\x([0-9a-f]{2})', lambda m: chr(int(m[1], 16)), scope)


def label(scope):
    """'app-niri-floorp-2240.scope' → 'floorp', 'app-md.Obsidian-2243.scope' → 'Obsidian'."""
    name = unescape(scope).removesuffix('.scope').removeprefix('app-').removeprefix('niri-')
    name = name.rpartition('-')[0] if name.rpartition('-')[2].isdigit() else name
    return name.rpartition('.')[2]


def write_state(state, path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)


class Stream:
    """A child process's stdout, read as it comes and started again if it exits."""

    def __init__(self, command, selector):
        self.command, self.selector, self.process, self.buffer, self.retry = command, selector, None, '', 0.0
        self.start()

    def start(self):
        self.buffer = ''
        try:
            self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ, self)
        except OSError as error:
            print(f'app-throttle: {self.command[0]}: {error}', file=sys.stderr)
            self.process, self.retry = None, time.monotonic() + RESCAN

    def read(self):
        chunk = self.process.stdout.read()
        if chunk:
            self.buffer += chunk.decode(errors='replace')
            return True
        self.selector.unregister(self.process.stdout)
        self.process.wait()
        self.process, self.retry = None, time.monotonic() + RESCAN  # It exited (niri or PipeWire restarting).
        return False

    def stop(self):
        if self.process:
            self.process.terminate()


def lines(stream):
    *complete, stream.buffer = stream.buffer.split('\n')
    return [json.loads(line) for line in complete if line.strip()]


def batches(stream):
    """pw-dump prints one JSON array per batch of changes."""
    decoder, out = json.JSONDecoder(), []
    while True:
        text = stream.buffer.lstrip()
        try:
            value, end = decoder.raw_decode(text)
        except ValueError:
            stream.buffer = text
            return out
        out.append(value)
        stream.buffer = text[end:]


def run(agent, niri_command=('niri', 'msg', '-j', 'event-stream'), pw_command=('pw-dump', '--monitor')):
    """Feed `agent` (a Session: Throttle, freeze.Freezer) niri's and PipeWire's
    events and act (its step()) after each batch, or every RESCAN; its state
    goes to agent.state_path. Stopping releases what it did."""
    selector = selectors.DefaultSelector()
    niri, pw = Stream(list(niri_command), selector), Stream(list(pw_command), selector)
    started, last = time.time(), None
    try:
        while True:
            for key, _ in selector.select(timeout=RESCAN):
                stream = key.data
                if not stream.read():
                    if stream is pw:
                        agent.nodes.clear()  # Unknown until it is back (apps are left alone meanwhile).
                    continue
                if stream is niri:
                    for event in lines(niri):
                        agent.window_event(event)
                else:
                    for batch in batches(pw):
                        agent.media_event(batch)
            for stream in (niri, pw):
                if stream.process is None and time.monotonic() >= stream.retry:
                    stream.start()
            state = {'pid': os.getpid(), 'since': started, **agent.step(pw.process is not None)}
            if state != last:
                write_state(state, agent.state_path)
                last = state
    finally:
        niri.stop()
        pw.stop()
        agent.release()
        agent.state_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='app-throttle', description=__doc__.split('\n\n')[0])
    parser.add_argument('--quota', type=int, default=QUOTA, help='%% of one core per background app (all its scopes)')
    parser.add_argument('--keep', action='append', help=f'scope patterns never throttled (default: {", ".join(KEEP)})')
    parser.add_argument('--release', action='store_true', help='lift every quota and exit')
    args = parser.parse_args(argv)
    if args.release:
        Throttle().release()
        STATE.unlink(missing_ok=True)
        return
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    run(Throttle(quota=max(1, min(args.quota, 100)), keep=args.keep if args.keep is not None else KEEP))


if __name__ == '__main__':
    main()
