#!/usr/bin/env python3
"""Quality of life, as thumbs: what stands out about the laptop right now.

👎 (the red thumb next to the battery, or `qol down`) says the laptop is worse
than it should be: automatic experiments end their block at once, restore your
settings and rest for COOLDOWN (`qol resume` ends that early), and the vote
teaches them which settings to avoid while you are there. 👍 (`qol up`) says
something is especially good — unexpectedly smooth, quiet, snappy or
long-lasting — which teaches them which settings are worth keeping (the
panel's Comfort goal). waybar-monitor logs every vote, as a `qol` event and in
the record it falls in.

    qol up | down           vote
    qol resume              end the rest after a 👎 now
    qol                     the last vote, and any rest left
    qol waybar up|down      JSON for Waybar's custom/qol-up and custom/qol-down
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

VOTES = ('down', 'up')
COOLDOWN = 10 * 60  # Seconds automatic experiments rest after a 👎 (`qol resume` ends it).
STATE = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'waybar-monitor/qol.json'
KEEP = 100  # Votes kept in the state file; the collector logs each one as it sees it.
WAYBAR_SIGNAL = 12  # custom/qol-up and custom/qol-down refresh on SIGRTMIN+12.
GLYPHS = {'up': '', 'down': ''}  # Font Awesome thumbs-up / thumbs-down.


def load(path=STATE):
    try:
        state = json.loads(Path(path).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state, path=STATE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    temporary.replace(path)
    subprocess.run(['pkill', f'-RTMIN+{WAYBAR_SIGNAL}', '-x', '.waybar-wrapped'], check=False)


def votes(path=STATE):
    """[(t, 'up' | 'down')], oldest first."""
    try:
        return [(float(v['t']), v['vote']) for v in load(path)['votes'] if v.get('vote') in VOTES]
    except (KeyError, TypeError, ValueError):
        return []


def vote(choice, path=STATE, now=None):
    if choice not in VOTES:
        raise ValueError(f'expected up or down, got {choice!r}')
    now = now or time.time()
    state = load(path)
    recent = votes(path)[-(KEEP - 1):] + [(now, choice)]
    state['votes'] = [{'t': t, 'vote': v} for t, v in recent]
    if choice == 'down':
        state['rest_until'] = now + COOLDOWN
    save(state, path)


def resume(path=STATE, now=None):
    state = load(path)
    state['rest_until'] = now or time.time()
    save(state, path)


def resting(path=STATE, now=None):
    """Seconds of rest left after the last 👎."""
    try:
        return max(0.0, float(load(path).get('rest_until', 0)) - (now or time.time()))
    except (TypeError, ValueError):
        return 0.0


def waybar(which, path=STATE, now=None):
    now = now or time.time()
    recent = votes(path)
    last = next((t for t, v in reversed(recent) if v == which), None)
    rest = resting(path, now)
    if which == 'down':
        tooltip = ('Thumbs down: the laptop is worse than it should be right now. Automatic experiments restore '
                   f'your settings and rest {COOLDOWN // 60} min, and learn what to avoid.')
        if rest:
            tooltip += f'\nResting {rest / 60:.0f} more min.'
    else:
        tooltip = ('Thumbs up: something is especially good right now (unexpectedly smooth, quiet, snappy or '
                   'long-lasting). Helps find settings worth keeping.')
    tooltip += '\nRight-click for the battery panel’s Experiments tab.'
    classes = [which] + (['recent'] if last and now - last < 15 else [])
    if rest and which == 'down':
        classes.append('resting')
    return {'text': GLYPHS[which], 'tooltip': tooltip, 'class': classes}


def main(argv):
    if not argv:
        recent = votes()
        rest = resting()
        print(f"last: {recent[-1][1]} at {time.strftime('%H:%M', time.localtime(recent[-1][0]))}" if recent
              else 'no votes yet', *([f'resting {rest / 60:.0f} min'] if rest else []), sep='; ')
    elif argv[0] in VOTES and len(argv) == 1:
        vote(argv[0])
    elif argv == ['resume']:
        resume()
    elif argv[0] == 'waybar' and len(argv) == 2 and argv[1] in VOTES:
        print(json.dumps(waybar(argv[1])))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
