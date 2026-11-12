"""The panel's model: python3 -m unittest discover -s panel -p 'test_*.py' (also the package's check)."""
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["TZ"] = "America/Los_Angeles"
time.tzset()

import dither_calendar_model as M  # noqa: E402


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def timed(title, start, end, **more):
    return {"key": title, "calendarId": "a", "title": title, "allDay": False, "startAt": ms(start), "endAt": ms(end),
            "startDate": None, "endDate": None, "status": "confirmed", "color": None, **more}


def allday(title, start, end, **more):
    return {"key": title, "calendarId": "a", "title": title, "allDay": True, "startAt": None, "endAt": None,
            "startDate": start, "endDate": end, "status": "confirmed", "color": None, **more}


CALS = [{"id": "a", "name": "Work", "color": "#111111", "selected": True},
        {"id": "b", "name": "Home", "color": "#222222", "selected": True},
        {"id": "off", "name": "Hidden", "color": "#333333", "selected": False}]


class Dates(unittest.TestCase):
    def test_years_as_the_bar_writes_them(self):
        self.assertEqual(M.year_text(2026), "12026")
        self.assertEqual(M.year_text(2026, holocene=False), "2026")
        self.assertEqual(M.month_title(2026, 11), "November 12026")
        self.assertEqual(M.long_date(date(2026, 11, 11)), "Wednesday, November 11, 12026")

    def test_month_grid_is_six_weeks_from_the_weeks_first_day(self):
        g = M.month_grid(2026, 11, 1)  # 1 November 2026 is a Sunday
        self.assertEqual((len(g), g[0], g[-1]), (42, date(2026, 10, 26), date(2026, 12, 6)))
        g = M.month_grid(2026, 11, 0)
        self.assertEqual((g[0], g[6]), (date(2026, 11, 1), date(2026, 11, 7)))
        self.assertEqual(M.weekday_heads(0)[0], "Su")
        self.assertEqual(M.weekday_heads(1)[0], "Mo")

    def test_words(self):
        self.assertEqual([M.span(x) for x in (20_000, 12 * M.MIN, 65 * M.MIN, 2 * M.HOUR, 50 * M.HOUR)],
                         ["under a minute", "12 min", "1 h 05 min", "2 h", "2 days"])
        today = date(2026, 11, 11)
        self.assertEqual([M.day_label(today + timedelta(days=d), today) for d in (0, 1, -1, 3)],
                         ["Today", "Tomorrow", "Yesterday", "Saturday 14 November"])
        self.assertEqual(M.day_label(date(2027, 1, 2), today), "Saturday 2 January 12027")


class Events(unittest.TestCase):
    def test_shown_leaves_out_hidden_calendars_cancelled_and_declined(self):
        evs = [timed("on", "2026-11-11T10:00", "2026-11-11T11:00"),
               timed("hidden", "2026-11-11T10:00", "2026-11-11T11:00", calendarId="off"),
               timed("cancelled", "2026-11-11T10:00", "2026-11-11T11:00", status="cancelled"),
               timed("declined", "2026-11-11T10:00", "2026-11-11T11:00", selfResponse="declined"),
               {"title": "rubbish", "calendarId": "a", "allDay": False, "startAt": None, "endAt": None}]
        self.assertEqual([e["title"] for e in M.shown(evs, CALS)], ["on"])

    def test_days_an_event_touches(self):
        self.assertEqual(M.days_of(allday("trip", "2026-11-13", "2026-11-16")),
                         [date(2026, 11, 13), date(2026, 11, 14), date(2026, 11, 15)])
        # Ends at midnight: the day it ends on is not one of its days. Past midnight: it is.
        self.assertEqual(M.days_of(timed("late", "2026-11-11T22:00", "2026-11-12T00:00")), [date(2026, 11, 11)])
        self.assertEqual(M.days_of(timed("later", "2026-11-11T22:00", "2026-11-12T01:00")),
                         [date(2026, 11, 11), date(2026, 11, 12)])

    def test_by_day_orders_all_day_first_and_dots_are_one_per_colour(self):
        evs = [timed("b", "2026-11-11T12:00", "2026-11-11T13:00", calendarId="b"),
               timed("a", "2026-11-11T09:00", "2026-11-11T10:00"),
               timed("a2", "2026-11-11T15:00", "2026-11-11T16:00"),
               allday("hol", "2026-11-11", "2026-11-12", color="#abcdef")]
        days = M.by_day(evs)
        self.assertEqual([e["title"] for e in days[date(2026, 11, 11)]], ["hol", "a", "b", "a2"])
        self.assertEqual(M.dots(days, date(2026, 11, 11), CALS), ["#abcdef", "#111111", "#222222"])
        self.assertEqual(M.dots(days, date(2026, 11, 12), CALS), [])

    def test_agenda_keeps_the_picked_day_and_skips_empty_ones(self):
        days = M.by_day([timed("x", "2026-11-13T09:00", "2026-11-13T10:00"),
                         timed("y", "2026-11-20T09:00", "2026-11-20T10:00")])
        got = M.agenda(days, date(2026, 11, 11))
        self.assertEqual([(d.day, len(evs)) for d, evs in got], [(11, 0), (13, 1), (20, 1)])
        self.assertEqual(len(M.agenda(days, date(2026, 11, 11), max_days=5)), 2)

    def test_now_and_next(self):
        evs = [timed("long", "2026-11-11T09:00", "2026-11-11T12:00"),
               timed("short", "2026-11-11T10:00", "2026-11-11T10:30"),
               timed("after", "2026-11-11T11:00", "2026-11-11T12:00"),
               allday("hol", "2026-11-11", "2026-11-12")]
        now = ms("2026-11-11T10:10")
        running, nxt = M.now_and_next(evs, now)
        self.assertEqual((running["title"], nxt["title"]), ("short", "after"))
        self.assertAlmostEqual(M.progress(running, now), 1 / 3, places=3)
        self.assertEqual(M.now_and_next(evs, ms("2026-11-11T13:00")), (None, None))

    def test_badges(self):
        e = timed("x", "2026-11-11T10:00", "2026-11-11T11:00")
        self.assertEqual(M.when_badge(e, ms("2026-11-11T10:30")), ("now", "now"))
        self.assertEqual(M.when_badge(e, ms("2026-11-11T09:52")), ("in 8 min", "soon"))
        self.assertEqual(M.when_badge(e, ms("2026-11-11T08:00")), ("in 2 h", "later"))
        self.assertEqual(M.when_badge(e, ms("2026-11-10T08:00")), ("", ""))
        self.assertEqual(M.when_badge(e, ms("2026-11-11T12:00")), ("", ""))
        self.assertEqual(M.time_range(e), "10:00 – 11:00")
        self.assertEqual(M.time_range(allday("d", "2026-11-11", "2026-11-12")), "All day")


class Plans(unittest.TestCase):
    def test_what_a_plan_says_and_its_buttons(self):
        p = {"allDay": False, "startAt": ms("2026-11-13T19:00"), "change": None}
        self.assertEqual(M.plan_actions(p), ("Fri 13 Nov 19:00", "Add", "Dismiss"))
        self.assertEqual(M.plan_actions({**p, "change": "moved"}), ("Moved to Fri 13 Nov 19:00", "Move it", "Keep"))
        self.assertEqual(M.plan_actions({**p, "change": "cancelled"}), ("Called off", "Remove", "Keep"))
        self.assertEqual(M.plan_when({"allDay": True, "startDate": "2026-11-14"}), "Sat 14 Nov")

    def test_a_quick_add_draft_in_words(self):
        d = {"allDay": False, "startAt": ms("2026-11-13T19:00"), "endAt": ms("2026-11-13T21:00"),
             "title": "Dinner with Alice", "location": "Luigi's"}
        self.assertEqual(M.draft_text(d), "Fri 13 Nov 19:00 – 21:00 · Dinner with Alice · Luigi's")
        self.assertEqual(M.draft_text({"allDay": True, "startDate": "2026-11-12", "title": "Dentist"}),
                         "Thu 12 Nov · Dentist")
        self.assertEqual(M.draft_text(None), "")


class Starting(unittest.TestCase):
    def test_a_browser_does_not_get_what_is_the_panels_own(self):
        env = {"PATH": "/bin", "WAYLAND_DISPLAY": "wayland-1", "LD_PRELOAD": "/nix/store/x/libgtk4-layer-shell.so",
               "PYTHONPATH": "/nix/store/y", "GI_TYPELIB_PATH": "/nix/store/z", "GDK_PIXBUF_MODULE_FILE": "/q"}
        self.assertEqual(M.child_env(env), {"PATH": "/bin", "WAYLAND_DISPLAY": "wayland-1"})
        self.assertIn("LD_PRELOAD", env)  # the panel's own is left as it was

    def test_how_a_url_is_opened(self):
        self.assertEqual(M.open_command("https://x/#/day/2026-11-12"),
                         ["systemd-run", "--user", "--scope", "--quiet", "--collect", "xdg-open", "https://x/#/day/2026-11-12"])
        self.assertEqual(M.open_command("https://x/", "stub", have_systemd_run=False), ["stub", "https://x/"])


class Kept(unittest.TestCase):
    def test_the_cache_is_for_one_server_and_survives_rubbish(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x" / "panel.json"
            self.assertIsNone(M.read_cache(path, "u"))
            M.write_cache(path, {"url": "u", "events": [], "from": "2026-10-26", "to": "2026-12-07"})
            self.assertEqual(M.read_cache(path, "u")["events"], [])
            self.assertIsNone(M.read_cache(path, "other"))
            path.write_text("not json {")
            self.assertIsNone(M.read_cache(path, "u"))

    def test_what_to_ask_for_and_whether_a_kept_answer_covers_it(self):
        today = date(2026, 11, 11)
        start, end = M.window_for(2026, 11, today)
        self.assertEqual((start, end), (date(2026, 10, 26), date(2026, 12, 7)))
        snap = {"from": start.isoformat(), "to": end.isoformat()}
        self.assertTrue(M.covers(snap, start, end))
        self.assertFalse(M.covers(snap, *M.window_for(2027, 2, today)))
        self.assertFalse(M.covers({}, start, end))
        # A month far ahead still asks for today's three weeks, for the card on top.
        self.assertEqual(M.window_for(2027, 2, today)[0], today - timedelta(days=1))

    def test_the_made_up_data_has_the_snapshots_shape(self):
        s = M.demo(ms("2026-11-11T14:20"))
        json.dumps(s)
        shown = M.shown(s["events"], s["calendars"])
        self.assertEqual(len(shown), len(s["events"]))
        running, nxt = M.now_and_next(shown, ms("2026-11-11T14:20"))
        self.assertEqual((running["title"], nxt["title"]), ("Design review", "1:1 with Sam"))


if __name__ == "__main__":
    unittest.main()
