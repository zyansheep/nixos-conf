"""What the calendar panel shows, worked out from the server's answers: no GTK here, so it can be tested alone.

The panel (dither-calendar-panel.py) asks the calendar for its state, the events around the month on show and the plans
waiting from chats, keeps the last answers on disk, and draws from that. Times are shown in this machine's own zone.
"""
import calendar
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_URL = "https://calendar.tn.zyancraft.net"
MIN = 60_000
HOUR = 60 * MIN
DAY_MS = 24 * HOUR

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


# ------------------------------------------------------------------ dates and words

def year_text(year, holocene=True):
    """The year as the bar's clock writes it: the Holocene era puts a 1 in front (12026)."""
    return str(year + 10000) if holocene else str(year)


def month_title(year, month, holocene=True):
    return f"{MONTHS[month - 1]} {year_text(year, holocene)}"


def month_grid(year, month, week_start=1):
    """The six weeks a month view shows, as 42 dates. `week_start`: 0 Sunday, 1 Monday (the calendar's setting)."""
    first = date(year, month, 1)
    # Python: Monday is 0. Days to step back to reach the week's first day.
    back = (first.weekday() - (0 if week_start == 1 else 6)) % 7
    start = first - timedelta(days=back)
    return [start + timedelta(days=i) for i in range(42)]


def weekday_heads(week_start=1):
    names = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    return names if week_start == 1 else names[6:] + names[:6]


def local(ms):
    return datetime.fromtimestamp(ms / 1000)


def hm(ms):
    return local(ms).strftime("%H:%M")


def span(ms):
    """A length of time in words a glance takes in: "under a minute", "12 min", "1 h 05 min", "3 days"."""
    m = int(ms // MIN)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    if m < 24 * 60:
        return f"{m // 60} h {m % 60:02d} min" if m % 60 else f"{m // 60} h"
    d = round(m / (24 * 60))
    return f"{d} day{'s' if d != 1 else ''}"


def day_label(d, today, holocene=True):
    """"Today", "Tomorrow", else "Saturday 14 November" (with the year when it is not this one)."""
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    if d == today - timedelta(days=1):
        return "Yesterday"
    text = f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    return text if d.year == today.year else f"{text} {year_text(d.year, holocene)}"


def long_date(d, holocene=True):
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}, {year_text(d.year, holocene)}"


# ------------------------------------------------------------------ events

def parse_date(s):
    return date.fromisoformat(s)


def shown(events, calendars):
    """The events worth showing: on calendars switched on in the web app, not cancelled, not declined by you."""
    on = {c["id"] for c in calendars if c.get("selected")}
    out = []
    for e in events:
        if on and e.get("calendarId") not in on:
            continue
        if e.get("status") == "cancelled" or e.get("selfResponse") == "declined":
            continue
        if e.get("allDay"):
            if not e.get("startDate") or not e.get("endDate"):
                continue
        elif not isinstance(e.get("startAt"), (int, float)) or not isinstance(e.get("endAt"), (int, float)):
            continue
        out.append(e)
    return out


def color_of(e, calendars):
    """#rrggbb: the event's own colour, else its calendar's."""
    if e.get("color"):
        return e["color"]
    for c in calendars:
        if c["id"] == e.get("calendarId"):
            return c.get("color") or "#8cb8f0"
    return "#8cb8f0"


def calendar_name(e, calendars):
    for c in calendars:
        if c["id"] == e.get("calendarId"):
            return c.get("name") or ""
    return ""


def days_of(e):
    """Every date an event touches. An all-day event's end date is exclusive, as Google has it; a timed event that
    ends at midnight does not count the day it ends on."""
    if e.get("allDay"):
        a, b = parse_date(e["startDate"]), parse_date(e["endDate"])
        b = max(b, a + timedelta(days=1))
    else:
        a = local(e["startAt"]).date()
        b = local(max(e["endAt"] - 1, e["startAt"])).date() + timedelta(days=1)
    n = min((b - a).days, 366)
    return [a + timedelta(days=i) for i in range(n)]


def order_key(e):
    """All-day first, then by start, then by title."""
    return (0 if e.get("allDay") else 1, e.get("startAt") or 0, (e.get("title") or "").lower())


def by_day(events):
    """date -> its events, in order."""
    days = {}
    for e in events:
        for d in days_of(e):
            days.setdefault(d, []).append(e)
    for evs in days.values():
        evs.sort(key=order_key)
    return days


def dots(days, d, calendars, limit=3):
    """Up to `limit` colours for the dots under a day's number: one per colour, in the day's order."""
    out = []
    for e in days.get(d, []):
        c = color_of(e, calendars)
        if c not in out:
            out.append(c)
        if len(out) == limit:
            break
    return out


def agenda(days, start, max_days=21, max_items=14):
    """The list beside the month: the day picked, then the days after it that have something, until the list is long
    enough. The picked day is always there, even when empty."""
    out = [(start, days.get(start, []))]
    count = len(out[0][1])
    for i in range(1, max_days):
        if count >= max_items:
            break
        d = start + timedelta(days=i)
        evs = days.get(d)
        if evs:
            out.append((d, evs))
            count += len(evs)
    return out


def now_and_next(events, now):
    """(the timed event running now that ends soonest, the next timed event to start), either may be None."""
    timed = sorted((e for e in events if not e.get("allDay")), key=lambda e: e["startAt"])
    running = sorted((e for e in timed if e["startAt"] <= now < e["endAt"]), key=lambda e: e["endAt"])
    nxt = next((e for e in timed if e["startAt"] > now), None)
    return (running[0] if running else None), nxt


def time_range(e):
    if e.get("allDay"):
        return "All day"
    if e["endAt"] == e["startAt"]:
        return hm(e["startAt"])
    return f"{hm(e['startAt'])} – {hm(e['endAt'])}"


def when_badge(e, now):
    """(text, kind) for the small mark at a row's right: how soon it starts or that it is on; ('', '') otherwise.
    kind: 'now', 'soon' (within 10 minutes), 'later' (within 12 hours)."""
    if e.get("allDay"):
        return "", ""
    if e["startAt"] <= now < e["endAt"]:
        return "now", "now"
    ahead = e["startAt"] - now
    if 0 < ahead <= 10 * MIN:
        return f"in {span(ahead)}", "soon"
    if 0 < ahead <= 12 * HOUR:
        return f"in {span(ahead)}", "later"
    return "", ""


def progress(e, now):
    """How far through a running event, 0..1."""
    length = max(1, e["endAt"] - e["startAt"])
    return min(1.0, max(0.0, (now - e["startAt"]) / length))


# ------------------------------------------------------------------ plans from chats

def plan_when(p):
    """"Fri 14 Nov 19:00", or "Fri 14 Nov" for a whole day."""
    if p.get("allDay") or p.get("startAt") is None:
        if not p.get("startDate"):
            return ""
        d = parse_date(p["startDate"])
        return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {MONTHS[d.month - 1][:3]}"
    t = local(p["startAt"])
    return f"{WEEKDAYS[t.weekday()][:3]} {t.day} {MONTHS[t.month - 1][:3]} {t.strftime('%H:%M')}"


def plan_actions(p):
    """(what it says, the accept button, the dismiss button) for a plan waiting for you."""
    if p.get("change") == "moved":
        return f"Moved to {plan_when(p)}", "Move it", "Keep"
    if p.get("change") == "cancelled":
        return "Called off", "Remove", "Keep"
    return plan_when(p), "Add", "Dismiss"


# ------------------------------------------------------------------ the server, and the copy kept on disk

def cache_file(env=None):
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "dither-calendar" / "panel.json"


def read_cache(path, url):
    try:
        c = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or c.get("url") != url or not isinstance(c.get("events"), list):
        return None
    return c


def write_cache(path, data):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass


class Client:
    """The calendar's HTTP API, as much of it as the panel uses. Calls block: the panel makes them off the main loop."""

    def __init__(self, url, timeout=8):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json", "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                said = json.loads(e.read().decode()).get("error")
            except Exception:
                said = None
            raise RuntimeError(said or f"the calendar said {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"could not reach the calendar ({getattr(e, 'reason', e)})") from None

    def snapshot(self, start, end):
        """Everything one drawing needs: the calendars and settings, the events overlapping [start, end) (dates),
        and the plans waiting. One dict, the shape the cache keeps."""
        a = int(datetime.combine(start, datetime.min.time()).timestamp() * 1000)
        b = int(datetime.combine(end, datetime.min.time()).timestamp() * 1000)
        state = self.call("GET", "/api/state")
        events = self.call("GET", f"/api/events?from={a}&to={b}")["events"]
        plans = self.call("GET", "/api/proposals?state=suggested")["proposals"]
        return {
            "url": self.url,
            "savedAt": int(datetime.now().timestamp() * 1000),
            "from": start.isoformat(),
            "to": end.isoformat(),
            "calendars": state.get("calendars", []),
            "weekStart": state.get("settings", {}).get("weekStart", 1),
            "waiting": state.get("waiting", len(plans)),
            "events": events,
            "plans": plans,
        }

    def answer(self, plan_id, how):
        return self.call("POST", f"/api/proposals/{plan_id}/{how}", {})

    def quick(self, text, create):
        return self.call("POST", "/api/quick", {"text": text, "create": create})


def window_for(year, month, today, week_start=1):
    """The dates to ask for: the month on show (its six weeks), and from today three weeks on for the list."""
    grid = month_grid(year, month, week_start)
    start = min(grid[0], today - timedelta(days=1))
    end = max(grid[-1] + timedelta(days=1), today + timedelta(days=22))
    return start, end


def covers(snap, start, end):
    """Whether a kept snapshot already holds [start, end)."""
    try:
        return parse_date(snap["from"]) <= start and end <= parse_date(snap["to"])
    except (KeyError, TypeError, ValueError):
        return False


def draft_text(d):
    """A quick-add draft in words: "Fri 13 Nov 19:00 – 21:00 · Dinner with Alice · Luigi's"."""
    if not d:
        return ""
    if d.get("allDay"):
        when = plan_when({"allDay": True, "startDate": d.get("startDate")})
    else:
        when = f"{plan_when({'startAt': d.get('startAt')})} – {hm(d['endAt'])}" if d.get("endAt") else plan_when(d)
    return " · ".join(x for x in (when, d.get("title"), d.get("location")) if x)


# ------------------------------------------------------------------ starting other programs

# What the panel's own wrapper sets so that GTK 4 and the layer shell load here. None of it is for a program the panel
# starts: a browser that inherits LD_PRELOAD loads GTK 4 beside its own GTK 3 and aborts at once.
OWN_ENV = ("LD_PRELOAD", "PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE", "GI_TYPELIB_PATH", "GDK_PIXBUF_MODULE_FILE",
           "GIO_EXTRA_MODULES", "GSETTINGS_SCHEMAS_PATH")


def child_env(env=None):
    """The environment for a program started from the panel (the browser): the panel's, without what is its own."""
    env = dict(os.environ if env is None else env)
    for name in OWN_ENV:
        env.pop(name, None)
    return env


def open_command(url, opener="xdg-open", have_systemd_run=True):
    """How a URL is opened: in a scope of its own where systemd is there, so the browser does not belong to the
    panel's unit and go with it."""
    cmd = [opener, url]
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", *cmd] if have_systemd_run else cmd


# ------------------------------------------------------------------ made-up data, for screenshots and trying it out

def demo(now_ms=None):
    """A week of made-up events around now, in the snapshot's shape."""
    # Without a time given: this afternoon, whatever the hour, so it looks the same whenever it is tried.
    now = local(now_ms) if now_ms else datetime.now().replace(hour=14, minute=20, second=0, microsecond=0)
    today = now.date()

    def at(day, h, m=0):
        return int(datetime.combine(today + timedelta(days=day), datetime.min.time()).replace(hour=h, minute=m).timestamp() * 1000)

    cals = [
        {"id": "work", "name": "Work", "color": "#62a0ea", "selected": True},
        {"id": "home", "name": "Personal", "color": "#57e389", "selected": True},
        {"id": "chat", "name": "From chats", "color": "#b79bff", "selected": True},
        {"id": "hol", "name": "Holidays", "color": "#f8e45c", "selected": True},
    ]
    n = int(now.timestamp() * 1000)
    ev = []

    def add(cal, title, start, end, **more):
        ev.append({"key": f"e{len(ev)}", "eventId": f"e{len(ev)}", "calendarId": cal, "title": title, "allDay": False,
                   "startAt": start, "endAt": end, "startDate": None, "endDate": None, "status": "confirmed",
                   "color": None, "location": None, "meetingUrl": None, "selfResponse": None, **more})

    def allday(cal, title, day, days=1):
        d = today + timedelta(days=day)
        ev.append({"key": f"e{len(ev)}", "eventId": f"e{len(ev)}", "calendarId": cal, "title": title, "allDay": True,
                   "startAt": None, "endAt": None, "startDate": d.isoformat(),
                   "endDate": (d + timedelta(days=days)).isoformat(), "status": "confirmed", "color": None,
                   "location": None, "meetingUrl": None, "selfResponse": None})

    add("work", "Design review", n - 20 * MIN, n + 25 * MIN, location="Room 2", meetingUrl="https://meet.example/abc")
    add("work", "1:1 with Sam", n + 34 * MIN, n + 64 * MIN, meetingUrl="https://meet.example/sam")
    add("home", "Climbing", n + 3 * HOUR, n + 5 * HOUR, location="The Depot")
    add("chat", "Dinner with Alice", at(0, 19, 30) if now.hour < 19 else n + 6 * HOUR, (at(0, 21, 30) if now.hour < 19 else n + 8 * HOUR), location="Luigi's")
    allday("hol", "Veterans Day", 0)
    add("work", "Standup", at(1, 9, 30), at(1, 9, 45), meetingUrl="https://meet.example/standup")
    add("home", "Dentist", at(1, 14), at(1, 15), location="12 Elm St")
    add("work", "Planning", at(2, 10), at(2, 11, 30))
    add("chat", "Board games", at(3, 18), at(3, 22), location="Jo's place")
    allday("home", "Trip to the coast", 5, 3)
    add("work", "Retro", at(6, 15), at(6, 16))
    for d in (-9, -6, -3, -2, 8, 9, 12, 13, 16, 19, 20):
        add("work" if d % 2 else "home", "Something", at(d, 10), at(d, 11))
    plans = [
        {"id": "p1", "title": "Ramen", "allDay": False, "startAt": at(2, 19), "startDate": None, "place": "Bea · Signal",
         "location": "Menya", "state": "suggested", "change": None},
        {"id": "p2", "title": "Board games", "allDay": False, "startAt": at(3, 19), "startDate": None,
         "place": "#plans in Friends", "location": None, "state": "added", "change": "moved"},
        {"id": "p3", "title": "Hike", "allDay": True, "startAt": None, "startDate": (today + timedelta(days=4)).isoformat(),
         "place": "Kai · WhatsApp", "location": None, "state": "suggested", "change": None},
    ]
    start, end = window_for(today.year, today.month, today)
    return {"url": "demo", "savedAt": n, "from": start.isoformat(), "to": end.isoformat(), "calendars": cals,
            "weekStart": 1, "waiting": 5, "events": ev, "plans": plans}
