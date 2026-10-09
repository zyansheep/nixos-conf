#!/usr/bin/env python3
"""Background-app throttle: while on battery, every app — a systemd scope in
app.slice — but the focused window's runs under a CPU quota of QUOTA% of one
core (cgroup cpu.max), lifted the moment it gets focus. Apps playing or
recording audio or video (PipeWire streams that are running, not just open)
and KEEP (T3 Code's agents, the input method that serves the focused app) are
left alone, and so is everything on AC.

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

QUOTA = 5  # % of one core per background app.
PERIOD = 100_000  # µs: cgroup cpu.max's period.
UNLIMITED = f'max {PERIOD}'
KEEP = ('t3code', 'fcitx5')  # Agents keep working; the input method types into the focused app.
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


class Throttle:
    def __init__(self, apps=APPS, quota=QUOTA, keep=KEEP, battery=on_battery, proc=Path('/proc')):
        self.apps, self.limit, self.keep = apps, f'{quota * PERIOD // 100} {PERIOD}', tuple(k.lower() for k in keep)
        self.battery, self.proc = battery, proc
        self.windows, self.focused, self.nodes = {}, None, {}  # window id → pid; PipeWire node id → what we know

    def window_event(self, event):
        """One line of `niri msg -j event-stream`."""
        if 'WindowsChanged' in event:
            windows = event['WindowsChanged']['windows']
            self.windows = {w['id']: w.get('pid') for w in windows}
            self.focused = next((w['id'] for w in windows if w.get('is_focused')), None)
        elif 'WindowOpenedOrChanged' in event:
            window = event['WindowOpenedOrChanged']['window']
            self.windows[window['id']] = window.get('pid')
            if window.get('is_focused'):
                self.focused = window['id']
        elif 'WindowClosed' in event:
            self.windows.pop(event['WindowClosed']['id'], None)
        elif 'WindowFocusChanged' in event:
            self.focused = event['WindowFocusChanged']['id']

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
        for line in (read(self.proc / str(pid) / 'cgroup') or '').splitlines():
            parts = line.rpartition(':')[2].split('/')
            if 'app.slice' in parts[:-1]:
                name = parts[parts.index('app.slice') + 1]
                return name if name.endswith('.scope') else None
        return None

    def wanted(self):
        """{scope: cpu.max} for every app scope."""
        scopes = sorted(p.name for p in self.apps.glob('app-*.scope'))
        if not self.battery():
            return dict.fromkeys(scopes, UNLIMITED)
        free = {self.scope_of(pid) for pid in [self.windows.get(self.focused), *self.media.values()] if pid}
        return {s: UNLIMITED if s in free or any(k in s.lower() for k in self.keep) else self.limit for s in scopes}

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


def label(scope):
    """'app-niri-floorp-2240.scope' → 'floorp', 'app-md.Obsidian-2243.scope' → 'Obsidian'."""
    name = re.sub(r'\\x([0-9a-f]{2})', lambda m: chr(int(m[1], 16)), scope)  # systemd's escapes
    name = name.removesuffix('.scope').removeprefix('app-').removeprefix('niri-')
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


def run(throttle, niri_command=('niri', 'msg', '-j', 'event-stream'), pw_command=('pw-dump', '--monitor')):
    selector = selectors.DefaultSelector()
    niri, pw = Stream(list(niri_command), selector), Stream(list(pw_command), selector)
    started, last = time.time(), None
    try:
        while True:
            for key, _ in selector.select(timeout=RESCAN):
                stream = key.data
                if not stream.read():
                    if stream is pw:
                        throttle.nodes.clear()  # Unknown until it is back (apps are left alone meanwhile).
                    continue
                if stream is niri:
                    for event in lines(niri):
                        throttle.window_event(event)
                else:
                    for batch in batches(pw):
                        throttle.media_event(batch)
            for stream in (niri, pw):
                if stream.process is None and time.monotonic() >= stream.retry:
                    stream.start()
            # Without stream data (PipeWire down), apps that might be playing stay unthrottled.
            throttled = throttle.apply() if pw.process else throttle.apply(dict.fromkeys(throttle.wanted(), UNLIMITED))
            state = {'pid': os.getpid(), 'since': started, 'quota': throttle.limit, 'on_battery': throttle.battery(),
                     'throttled': sorted({label(s) for s in throttled}, key=str.lower)}
            if state != last:
                write_state(state)
                last = state
    finally:
        niri.stop()
        pw.stop()
        throttle.release()
        STATE.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='app-throttle', description=__doc__.split('\n\n')[0])
    parser.add_argument('--quota', type=int, default=QUOTA, help='%% of one core per background app')
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
