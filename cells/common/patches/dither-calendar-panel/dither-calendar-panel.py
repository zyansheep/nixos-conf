#!/usr/bin/env python3
"""dither-calendar's panel under the bar: a month, what is on now and next, the days ahead, the plans waiting from
chats, and a line to add an event in words. The bar's clock opens it (docs/panel.md).

A resident GTK 4 layer-shell window, toggled over D-Bus so it opens at once:

    dither-calendar-panel                 start as a service (org.zyansheep.CalendarPanel) and wait
    gdbus call --session --dest org.zyansheep.CalendarPanel --object-path /org/zyansheep/CalendarPanel \
        --method org.gtk.Actions.Activate toggle '[]' '{}'

    --url <calendar>   or DITHER_CALENDAR_URL   (default https://calendar.tn.zyancraft.net)
    --gregorian        years as 2026; the default follows the bar's clock, 12026
    --offset <px>      or DITHER_CALENDAR_PANEL_OFFSET: how far right of the screen's middle to open (negative: left).
                       Waybar's clock is pushed off centre when the modules on one side are wider, so this lines the
                       panel up under it. With several monitors it is ignored and the panel opens in the middle.
    --standalone       no D-Bus name: open now, quit when closed (trying it out)
    --demo             made-up events, no server
    --keep-open        do not close when the pointer clicks elsewhere (screenshots)
    --type <words>     put these words in the add line once shown, to see what the calendar makes of them
    --pick <date>      open with this day picked (YYYY-MM-DD)

Hidden, it does nothing at all: no timer and no connection. Shown, it asks the calendar once, then every two minutes.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

# GTK and the layer shell are loaded: the preload that put the layer shell first has done its work, and must not reach
# anything started from here (a GTK 3 browser aborts with it).
os.environ.pop("LD_PRELOAD", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dither_calendar_model as M  # noqa: E402

APP_ID = "org.zyansheep.CalendarPanel"
ACCENT = "#3584e4"
LEFT_W = 296
RIGHT_W = 404

# The palette of the other panels under this bar (dotfiles/.config/waybar/menu-theme.css).
CSS = """
window { background: transparent; }
#cal-card { background: #222226; color: #ffffff; border: 1px solid rgba(255, 255, 255, 0.09); border-radius: 16px;
            padding: 16px 18px 16px 18px; font-family: sans-serif; font-size: 14px; }
#cal-card label { color: inherit; }
#cal-card .muted { color: rgba(255, 255, 255, 0.62); }
#cal-card .kicker { font-size: 10.5px; font-weight: 700; letter-spacing: 1.6px; color: rgba(255, 255, 255, 0.5); }
#cal-card .headline { font-size: 19px; font-weight: 700; }
#cal-card .status { font-size: 12px; color: rgba(255, 255, 255, 0.5); }
#cal-card .status.offline { color: #f8e45c; }
#cal-card .month-title { font-size: 16px; font-weight: 700; }
#cal-card separator { background: rgba(255, 255, 255, 0.08); min-height: 1px; min-width: 1px; }

#cal-card button { box-shadow: none; outline-offset: -2px; }
#cal-card button.icon { min-width: 30px; min-height: 30px; padding: 0; border-radius: 9px; background: transparent;
                        color: rgba(255, 255, 255, 0.8); }
#cal-card button.icon:hover { background: rgba(255, 255, 255, 0.1); color: #ffffff; }
#cal-card button.pill { min-height: 26px; padding: 0 12px; border-radius: 999px; font-size: 12px; font-weight: 700;
                        background: rgba(255, 255, 255, 0.09); color: #ffffff; }
#cal-card button.pill:hover { background: rgba(255, 255, 255, 0.16); }
#cal-card button.pill.primary { background: #3584e4; }
#cal-card button.pill.primary:hover { background: #4a94ea; }
#cal-card button.pill.quiet { background: transparent; color: rgba(255, 255, 255, 0.7); }
#cal-card button.pill.quiet:hover { background: rgba(255, 255, 255, 0.1); color: #ffffff; }
#cal-card button.link { min-height: 22px; padding: 0 2px; background: transparent; font-size: 12px; font-weight: 600;
                        color: #78aeed; }
#cal-card button.link:hover { color: #a9cbf5; }
#cal-card button.earlier { min-height: 24px; margin: 1px 0; padding: 0 8px; border-radius: 8px; background: transparent;
                           font-size: 12px; font-weight: 500; color: rgba(255, 255, 255, 0.5); }
#cal-card button.earlier:hover { background: rgba(255, 255, 255, 0.07); color: #ffffff; }

#cal-card .hero { border-radius: 14px; padding: 13px 14px 14px 14px; background: #343437;
                  border: 1px solid rgba(255, 255, 255, 0.06); }
#cal-card .hero .title { font-size: 18px; font-weight: 700; }
#cal-card .hero .big { font-size: 21px; font-weight: 300; font-feature-settings: "tnum"; }
#cal-card .hero .sub { font-size: 13px; color: rgba(255, 255, 255, 0.72); }
#cal-card .hero progressbar trough { min-height: 4px; border-radius: 2px; background: rgba(255, 255, 255, 0.14); }
#cal-card .hero progressbar progress { min-height: 4px; border-radius: 2px; background: #ffffff; }

#cal-card .day-head { margin-top: 12px; margin-bottom: 2px; padding: 0 8px; }
#cal-card .day-head .date { font-size: 11.5px; color: rgba(255, 255, 255, 0.4); }
#cal-card .row { border-radius: 10px; padding: 6px 8px; }
#cal-card .row:hover { background: rgba(255, 255, 255, 0.07); }
#cal-card .row.past { opacity: 0.42; }
#cal-card .row .time { font-size: 13px; font-weight: 600; font-feature-settings: "tnum"; }
#cal-card .row .end { font-size: 11.5px; color: rgba(255, 255, 255, 0.5); font-feature-settings: "tnum"; }
#cal-card .row .title { font-weight: 600; }
#cal-card .row .sub { font-size: 12px; color: rgba(255, 255, 255, 0.58); }
#cal-card .bar { min-width: 4px; border-radius: 2px; background: #8cb8f0; }
#cal-card .empty { padding: 6px 8px; color: rgba(255, 255, 255, 0.45); font-size: 13px; }

#cal-card .badge { border-radius: 999px; padding: 1px 9px; font-size: 11.5px; font-weight: 700;
                   font-feature-settings: "tnum"; }
#cal-card .badge.now { background: rgba(98, 160, 234, 0.26); color: #a9cbf5; }
#cal-card .badge.soon { background: rgba(248, 228, 92, 0.2); color: #f9f06b; }
#cal-card .badge.later { padding: 1px 0; font-weight: 500; color: rgba(255, 255, 255, 0.5); }
#cal-card .count { border-radius: 999px; padding: 0 8px; font-size: 11px; font-weight: 700;
                   background: rgba(183, 155, 255, 0.24); color: #d5c6ff; }

#cal-card .plan { border-radius: 12px; padding: 9px 10px 9px 12px; background: #2e2e32; }
#cal-card .plan .title { font-weight: 700; }
#cal-card .plan .sub { font-size: 12px; color: rgba(255, 255, 255, 0.6); }

#cal-card entry.quick { min-height: 38px; border-radius: 11px; padding: 0 12px; background: #2e2e32; color: #ffffff;
                        border: 1px solid rgba(255, 255, 255, 0.07); outline: none; box-shadow: none; }
#cal-card entry.quick:focus-within { border-color: rgba(53, 132, 228, 0.9); }
#cal-card entry.quick image { color: rgba(255, 255, 255, 0.55); }
#cal-card .preview { font-size: 12px; padding: 0 4px; color: rgba(255, 255, 255, 0.55); }
#cal-card .preview.good { color: #8ff0a4; }
#cal-card .preview.bad { color: #f8e45c; }
"""


def log(*words):
    """One line to stderr (the journal, under the panel's unit): what was asked of it, and what went wrong."""
    print("calendar-panel:", *words, file=sys.stderr, flush=True)


def rgb(hex_color):
    h = hex_color.lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, IndexError):
        return (0.55, 0.72, 0.94)


def label(text="", *classes, xalign=0.0, wrap=False, ellipsize=True, markup=False, **props):
    w = Gtk.Label(xalign=xalign, **props)
    if markup:
        w.set_markup(text)
    else:
        w.set_text(text)
    if wrap:
        w.set_wrap(True)
        w.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    elif ellipsize:
        w.set_ellipsize(Pango.EllipsizeMode.END)
    for c in classes:
        w.add_css_class(c)
    return w


def button(text, *classes, on=None, icon=None, tip=None):
    b = Gtk.Button.new_from_icon_name(icon) if icon else Gtk.Button(label=text)
    for c in classes:
        b.add_css_class(c)
    if tip:
        b.update_property([Gtk.AccessibleProperty.LABEL], [tip])
    if on:
        b.connect("clicked", lambda *_: on())
    b.set_valign(Gtk.Align.CENTER)
    b.set_focus_on_click(False)
    return b


def under(e, calendars):
    """The line under an event: where it is; else which calendar, unless that is named by an e-mail address."""
    where = (e.get("location") or "").strip()
    if where and where != e.get("meetingUrl") and not where.startswith(("http://", "https://")):
        return where
    name = M.calendar_name(e, calendars)
    return "" if "@" in name else name


def clear(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class MonthGrid(Gtk.DrawingArea):
    """The month, drawn: today filled, the picked day ringed, a dot per calendar that has something that day."""

    HEAD_H = 24
    CELL_H = 40

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.hover = None
        self.set_content_width(LEFT_W)
        self.set_content_height(self.HEAD_H + 6 * self.CELL_H)
        self.set_draw_func(self.draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self.pressed)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self.set_hover(self.cell_at(x, y)))
        motion.connect("leave", lambda _c: self.set_hover(None))
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", lambda _c, _dx, dy: (self.panel.step_month(1 if dy > 0 else -1), True)[1])
        self.add_controller(scroll)

    def cell_at(self, x, y):
        if y < self.HEAD_H:
            return None
        col, row = int(x // (self.get_width() / 7)), int((y - self.HEAD_H) // self.CELL_H)
        return row * 7 + col if 0 <= col < 7 and 0 <= row < 6 else None

    def set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.queue_draw()

    def pressed(self, _g, _n, x, y):
        i = self.cell_at(x, y)
        if i is not None:
            self.panel.pick(self.panel.grid[i])

    def text(self, cr, layout, s, cx, cy, color, size=13, bold=False):
        font = Pango.FontDescription()
        font.set_family("sans-serif")
        font.set_absolute_size(size * Pango.SCALE)
        font.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(font)
        layout.set_text(s, -1)
        w, h = layout.get_pixel_size()
        cr.set_source_rgba(*color)
        cr.move_to(cx - w / 2, cy - h / 2)
        PangoCairo.show_layout(cr, layout)

    def draw(self, _area, cr, width, _height):
        p = self.panel
        layout = PangoCairo.create_layout(cr)
        cw = width / 7
        for i, name in enumerate(M.weekday_heads(p.week_start)):
            self.text(cr, layout, name.upper(), cw * i + cw / 2, self.HEAD_H / 2 - 2, (1, 1, 1, 0.42), size=10, bold=True)
        accent = rgb(ACCENT)
        for i, d in enumerate(p.grid):
            cx = cw * (i % 7) + cw / 2
            cy = self.HEAD_H + self.CELL_H * (i // 7) + self.CELL_H / 2 - 4
            inside = d.month == p.view[1]
            today, picked = d == p.today, d == p.selected
            if today:
                cr.set_source_rgba(*accent, 1)
                cr.arc(cx, cy, 13.5, 0, 6.2832)
                cr.fill()
            elif picked:
                cr.set_source_rgba(1, 1, 1, 0.13)
                cr.arc(cx, cy, 13.5, 0, 6.2832)
                cr.fill()
            elif i == self.hover:
                cr.set_source_rgba(1, 1, 1, 0.07)
                cr.arc(cx, cy, 13.5, 0, 6.2832)
                cr.fill()
            if picked and not today:
                cr.set_source_rgba(1, 1, 1, 0.5)
                cr.set_line_width(1.5)
                cr.arc(cx, cy, 13.5, 0, 6.2832)
                cr.stroke()
            alpha = 1.0 if today else 0.92 if inside else 0.26
            self.text(cr, layout, str(d.day), cx, cy, (1, 1, 1, alpha), bold=today)
            colors = M.dots(p.days, d, p.calendars)
            x = cx - (len(colors) - 1) * 3.5
            for c in colors:
                cr.set_source_rgba(*rgb(c), 1.0 if inside else 0.4)
                cr.arc(x, cy + 19.5, 2.2, 0, 6.2832)
                cr.fill()
                x += 7


class Panel(Adw.Application):
    def __init__(self, url, holocene=True, standalone=False, demo=False, keep_open=False, type_words="", pick_day=None, offset=0):
        flags = Gio.ApplicationFlags.NON_UNIQUE if standalone else Gio.ApplicationFlags.IS_SERVICE
        super().__init__(application_id=APP_ID, flags=flags)
        self.url, self.holocene, self.standalone, self.demo, self.keep_open = url.rstrip("/"), holocene, standalone, demo, keep_open
        self.client = M.Client(self.url)
        self.cache = M.cache_file()
        self.snap = M.demo() if demo else M.read_cache(self.cache, self.url)
        self.online = None  # None: not asked yet
        self.error = None
        self.fetching = False
        self.again = False
        self.busy_plans = set()
        self.today = date.today()
        self.selected = self.today
        self.view = (self.today.year, self.today.month)
        self.window = None
        self.focused_once = False
        self.tick_timer = 0
        self.ticks = 0
        self.quick_timer = 0
        self.quick_serial = 0
        self.colors_done = set()
        self.show_earlier = False
        self.hero_key = None
        self.type_words, self.pick_day, self.offset = type_words, pick_day, offset
        self.derive()
        self.connect("startup", self.startup)
        self.connect("activate", lambda *_: self.toggle())

    # ------------------------------------------------------------ what there is to show

    def derive(self):
        s = self.snap or {}
        self.calendars = s.get("calendars", [])
        self.week_start = s.get("weekStart", 1)
        self.events = M.shown(s.get("events", []), self.calendars)
        self.days = M.by_day(self.events)
        self.plans = s.get("plans", [])
        self.waiting = s.get("waiting", len(self.plans))
        self.grid = M.month_grid(self.view[0], self.view[1], self.week_start)

    def now_ms(self):
        return self.snap["savedAt"] if self.demo else int(time.time() * 1000)

    def refresh(self):
        if self.demo:
            return
        if self.fetching:
            self.again = True
            return
        log("ask", self.url)
        self.fetching = True
        start, end = M.window_for(self.view[0], self.view[1], self.today, self.week_start)

        def work():
            try:
                snap, err = self.client.snapshot(start, end), None
            except RuntimeError as e:
                snap, err = None, str(e)
            GLib.idle_add(self.loaded, snap, err)

        threading.Thread(target=work, daemon=True).start()

    def loaded(self, snap, err):
        self.fetching = False
        if snap:
            self.snap, self.online, self.error = snap, True, None
            M.write_cache(self.cache, snap)
        else:
            self.online, self.error = False, err
            log("no answer:", err)
        self.derive()
        self.render()
        if self.again:
            self.again = False
            self.refresh()
        return False

    # ------------------------------------------------------------ the window

    def startup(self, *_):
        self.hold()
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        act = Gio.SimpleAction.new("toggle", None)
        act.connect("activate", lambda *_: self.toggle())
        self.add_action(act)
        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.color_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.color_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.build()
        self.render()
        # Laid out once now, so the first click opens at once.
        # (Standalone, GApplication activates right after this, which shows it.)
        self.window.realize()
        self.resize()

    def build(self):
        w = self.window = Adw.ApplicationWindow(application=self, title="Calendar")
        w.connect("close-request", lambda *_: self.close())
        w.connect("notify::is-active", self.focus_changed)
        w.set_decorated(False)
        w.set_default_size(1, 1)
        LayerShell.init_for_window(w)
        LayerShell.set_namespace(w, "calendar-panel")
        LayerShell.set_layer(w, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(w, LayerShell.KeyboardMode.ON_DEMAND)
        LayerShell.set_exclusive_zone(w, -1)
        # Top only: centred under the clock in the middle of the bar.
        LayerShell.set_anchor(w, LayerShell.Edge.TOP, True)
        LayerShell.set_margin(w, LayerShell.Edge.TOP, 30)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key)
        w.add_controller(keys)

        card = self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.set_name("cal-card")

        # Header: today's date as the bar writes it, whether the calendar answered, open in the browser, close.
        head = Gtk.Box(spacing=8)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self.headline = label("", "headline", markup=True)
        self.status = label("", "status")
        titles.append(self.headline)
        titles.append(self.status)
        head.append(titles)
        head.append(button("Open calendar", "pill", on=lambda: self.open_web("")))
        head.append(button("", "icon", icon="view-refresh-symbolic", tip="Ask the calendar again", on=self.refresh))
        head.append(button("", "icon", icon="window-close-symbolic", tip="Close", on=self.close))
        card.append(head)
        card.append(Gtk.Separator())

        cols = Gtk.Box(spacing=20)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(LEFT_W, -1)
        nav = Gtk.Box(spacing=2)
        self.month_title = label("", "month-title", hexpand=True)
        nav.append(self.month_title)
        self.today_btn = button("Today", "pill", "quiet", on=lambda: self.pick(date.today()))
        nav.append(self.today_btn)
        nav.append(button("", "icon", icon="go-previous-symbolic", tip="The month before", on=lambda: self.step_month(-1)))
        nav.append(button("", "icon", icon="go-next-symbolic", tip="The month after", on=lambda: self.step_month(1)))
        left.append(nav)
        self.month = MonthGrid(self)
        left.append(self.month)
        self.plans_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.append(self.plans_box)
        cols.append(self.column(left, LEFT_W))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_size_request(RIGHT_W, -1)
        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.append(self.hero)
        self.agenda = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = self.scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(380)
        scroll.set_child(self.agenda)
        right.append(scroll)
        cols.append(self.column(right, RIGHT_W))
        card.append(cols)

        # Add in words: the calendar's own parser says what it understood as you type; Enter adds it.
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.quick = Gtk.Entry(placeholder_text="Add in words:  dinner with Alice friday 7pm at Luigi's")
        self.quick.add_css_class("quick")
        self.quick.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "list-add-symbolic")
        self.quick.connect("changed", self.quick_changed)
        self.quick.connect("activate", self.quick_add)
        foot.append(self.quick)
        self.preview = label("", "preview")
        self.preview.set_visible(False)
        foot.append(self.preview)
        card.append(foot)
        w.set_content(card)

    @staticmethod
    def column(child, width):
        clamp = Adw.Clamp(maximum_size=width, tightening_threshold=width, child=child)
        clamp.set_size_request(width, -1)
        return clamp

    def place(self):
        """Where it opens: the middle of the screen, or `offset` pixels from it (docs/panel.md). The layer shell
        centres a window anchored only at the top; one moved off centre is anchored at the left with a margin."""
        monitors = Gdk.Display.get_default().get_monitors()
        if not self.offset or monitors.get_n_items() != 1:
            LayerShell.set_anchor(self.window, LayerShell.Edge.LEFT, False)
            return
        screen = monitors.get_item(0).get_geometry().width
        width = self.card.measure(Gtk.Orientation.HORIZONTAL, -1).natural
        left = (screen - width) // 2 + self.offset
        LayerShell.set_anchor(self.window, LayerShell.Edge.LEFT, True)
        LayerShell.set_margin(self.window, LayerShell.Edge.LEFT, max(8, min(screen - width - 8, left)))

    def resize(self):
        width = self.card.measure(Gtk.Orientation.HORIZONTAL, -1).natural
        height = self.card.measure(Gtk.Orientation.VERTICAL, width).natural
        self.window.set_default_size(width, height)

    def color_class(self, color):
        """A CSS class for a calendar's colour (the bar beside a row, the tint of the card on top)."""
        c = color.lstrip("#").lower()
        if len(c) != 6 or any(ch not in "0123456789abcdef" for ch in c):
            c = "8cb8f0"
        if c not in self.colors_done:
            self.colors_done.add(c)
            rules = "".join(
                f"#cal-card .bar.c-{x} {{ background: #{x}; }}"
                f"#cal-card .hero.c-{x} {{ background: linear-gradient(120deg, alpha(#{x}, 0.34), alpha(#{x}, 0.10)); border-color: alpha(#{x}, 0.35); }}"
                f"#cal-card .hero.c-{x} progressbar progress {{ background: #{x}; }}"
                for x in sorted(self.colors_done))
            self.color_css.load_from_data(rules.encode())
        return f"c-{c}"

    # ------------------------------------------------------------ drawing what there is

    def render(self):
        now = self.now_ms()
        self.today = date.today()
        t = self.today
        self.headline.set_markup(
            f"{M.WEEKDAYS[t.weekday()]}, {M.MONTHS[t.month - 1]} {t.day}  "
            f"<span weight='300' alpha='55%'>{M.year_text(t.year, self.holocene)}</span>")
        self.render_status()
        self.month_title.set_text(M.month_title(self.view[0], self.view[1], self.holocene))
        self.today_btn.set_visible(self.selected != t or self.view != (t.year, t.month))
        self.month.queue_draw()
        self.render_hero(now)
        self.render_agenda(now)
        self.render_plans()
        if self.window and self.window.get_realized():
            GLib.idle_add(lambda: (self.resize(), False)[1])

    def render_status(self):
        self.status.remove_css_class("offline")
        if self.demo:
            self.status.set_text("Made-up events")
        elif self.online is False:
            self.status.add_css_class("offline")
            saved = self.snap and self.snap.get("savedAt")
            self.status.set_text(f"Offline · as it was at {M.hm(saved)}" if saved else f"Offline · {self.error or 'no answer'}")
        elif self.snap:
            n = len(self.days.get(self.today, []))
            self.status.set_text("Nothing today" if n == 0 else f"{n} event{'s' if n != 1 else ''} today")
        else:
            self.status.set_text("Asking the calendar…")

    def render_hero(self, now):
        clear(self.hero)
        self.hero_key = None
        for c in list(self.hero.get_css_classes()):
            self.hero.remove_css_class(c)
        self.hero.add_css_class("hero")
        running, nxt = M.now_and_next(self.events, now)
        e = running or nxt
        if not e:
            self.hero.append(label("CLEAR", "kicker", ellipsize=False))
            self.hero.append(label("Nothing coming up", "title"))
            self.hero.append(label("No events in the next three weeks." if self.snap else "The calendar has not answered yet.", "sub"))
            return
        self.hero.add_css_class(self.color_class(M.color_of(e, self.calendars)))
        top = Gtk.Box(spacing=8)
        day = M.local(e["startAt"]).date()
        if running:
            kicker, big = "NOW", f"{M.span(e['endAt'] - now)} left"
        elif e["startAt"] - now <= 12 * M.HOUR:
            kicker, big = "NEXT", f"in {M.span(e['startAt'] - now)}"
        else:
            kicker, big = "NEXT", M.day_label(day, self.today, self.holocene)
        top.append(label(kicker, "kicker", hexpand=True, valign=Gtk.Align.CENTER, ellipsize=False))
        top.append(label(big, "big", xalign=1.0))
        self.hero_key = e.get("key")
        self.hero.append(top)
        title = label((e.get("title") or "(No title)").strip(), "title", wrap=True)
        title.set_lines(2)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        self.hero.append(title)
        bits = [M.time_range(e), under(e, self.calendars)]
        line = Gtk.Box(spacing=8)
        line.append(label(" · ".join(b for b in bits if b), "sub", hexpand=True))
        # A way in while it is on, or about to be.
        if e.get("meetingUrl") and e["startAt"] - now <= 15 * M.MIN:
            line.append(button("Join", "pill", "primary", on=lambda u=e["meetingUrl"]: self.open_url(u)))
        self.hero.append(line)
        if running:
            bar = Gtk.ProgressBar(fraction=M.progress(e, now), margin_top=6)
            self.hero.append(bar)
            if nxt and nxt["startAt"] - e["endAt"] <= 60 * M.MIN:
                self.hero.append(label(f"Then {nxt.get('title') or '(No title)'} at {M.hm(nxt['startAt'])}", "sub", margin_top=4))

    def render_agenda(self, now):
        clear(self.agenda)
        if not self.snap:
            self.agenda.append(label(self.error or "Asking the calendar…", "empty", wrap=True))
            return
        for d, evs in M.agenda(self.days, self.selected):
            head = Gtk.Box(spacing=8)
            head.add_css_class("day-head")
            name = M.day_label(d, self.today, self.holocene)
            head.append(label(name.upper(), "kicker", ellipsize=False))
            if name in ("Today", "Tomorrow", "Yesterday"):
                head.append(label(f"{M.WEEKDAYS[d.weekday()][:3]} {d.day} {M.MONTHS[d.month - 1][:3]}", "date"))
            self.agenda.append(head)
            if not evs:
                self.agenda.append(label("Nothing planned", "empty"))
            over = [e for e in evs if not e.get("allDay") and e["endAt"] <= now] if d == self.today else []
            fold = None
            if over and not self.show_earlier:
                evs = [e for e in evs if e not in over]
                fold = button(f"{len(over)} earlier today", "earlier", on=self.unfold_earlier)
                fold.set_halign(Gtk.Align.START)
            for e in (e for e in evs if e.get("allDay")):
                self.agenda.append(self.row(e, d, now))
            if fold:
                self.agenda.append(fold)
                if all(e.get("allDay") for e in evs):
                    self.agenda.append(label("Nothing more today", "empty"))
            for e in (e for e in evs if not e.get("allDay")):
                self.agenda.append(self.row(e, d, now))

    def row(self, e, d, now):
        box = Gtk.Box(spacing=10)
        box.add_css_class("row")
        if not e.get("allDay") and e["endAt"] <= now:
            box.add_css_class("past")
        bar = Gtk.Box()
        bar.add_css_class("bar")
        bar.add_css_class(self.color_class(M.color_of(e, self.calendars)))
        box.append(bar)
        times = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        times.set_size_request(46, -1)
        if e.get("allDay"):
            times.append(label("all day", "end"))
        else:
            # A timed event shown on a later day than it starts (it runs past midnight) says so instead of a time.
            starts_here = M.local(e["startAt"]).date() == d
            times.append(label(M.hm(e["startAt"]) if starts_here else "until", "time"))
            times.append(label(M.hm(e["endAt"]), "end"))
        box.append(times)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label((e.get("title") or "(No title)").strip(), "title"))
        sub = under(e, self.calendars)
        if sub:
            text.append(label(sub, "sub"))
        box.append(text)
        # How soon, on the day it starts (a row for the day after an event that runs past midnight says nothing).
        words, kind = M.when_badge(e, now) if e.get("allDay") or M.local(e["startAt"]).date() == d or e["startAt"] <= now else ("", "")
        if words:
            box.append(label(words, "badge", kind, valign=Gtk.Align.CENTER, ellipsize=False))
        # The card on top already has the button for its own event.
        if e.get("meetingUrl") and kind in ("now", "soon") and e.get("key") != self.hero_key:
            box.append(button("Join", "pill", "primary", on=lambda u=e["meetingUrl"]: self.open_url(u)))
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.open_web(f"#/day/{d.isoformat()}"))
        box.add_controller(click)
        box.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        return box

    def unfold_earlier(self):
        self.show_earlier = True
        self.render()

    def render_plans(self):
        clear(self.plans_box)
        if not self.plans:
            return
        self.plans_box.append(Gtk.Separator(margin_top=6, margin_bottom=6))
        head = Gtk.Box(spacing=8)
        head.append(label("FROM YOUR CHATS", "kicker", ellipsize=False))
        head.append(label(str(self.waiting), "count", ellipsize=False))
        head.append(Gtk.Box(hexpand=True))
        if self.waiting > 2:
            head.append(button(f"All {self.waiting}", "link", on=lambda: self.open_web("#/chats")))
        self.plans_box.append(head)
        for p in self.plans[:2]:
            says, yes, no = M.plan_actions(p)
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.add_css_class("plan")
            card.append(label(p.get("title") or "(No title)", "title"))
            card.append(label(" · ".join(b for b in (says, p.get("place")) if b), "sub"))
            acts = Gtk.Box(spacing=6)
            busy = p["id"] in self.busy_plans or self.online is False
            for text, how, cls in ((yes, "accept", ("pill", "primary")), (no, "dismiss", ("pill",))):
                b = button(text, *cls, on=lambda pid=p["id"], how=how: self.answer(pid, how))
                b.set_sensitive(not busy)
                acts.append(b)
            card.append(acts)
            self.plans_box.append(card)

    # ------------------------------------------------------------ what you do

    def pick(self, d):
        log("pick", d.isoformat())
        self.selected = d
        if (d.year, d.month) != self.view:
            self.set_view(d.year, d.month)
        else:
            self.render()

    def step_month(self, by):
        log("month", by)
        y, m = self.view
        m += by
        y, m = (y - 1, 12) if m < 1 else (y + 1, 1) if m > 12 else (y, m)
        self.set_view(y, m)

    def set_view(self, y, m):
        self.view = (y, m)
        self.grid = M.month_grid(y, m, self.week_start)
        self.render()
        start, end = M.window_for(y, m, self.today, self.week_start)
        if not self.snap or not M.covers(self.snap, start, end):
            self.refresh()

    def key(self, _c, keyval, _code, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if self.quick.has_focus() or self.quick.get_delegate().has_focus():
            return False
        steps = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -7, Gdk.KEY_Down: 7}
        if keyval in steps:
            self.pick(self.selected + timedelta(days=steps[keyval]))
            return True
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            self.step_month(-1 if keyval == Gdk.KEY_Page_Up else 1)
            return True
        if keyval in (Gdk.KEY_Home, Gdk.KEY_t):
            self.pick(date.today())
            return True
        return False

    def answer(self, plan_id, how):
        if self.demo:
            self.say("Made-up events: nothing changes.", "bad")
            return
        log("plan", how, plan_id)
        self.busy_plans.add(plan_id)
        self.render_plans()

        def work():
            try:
                self.client.answer(plan_id, how)
                err = None
            except RuntimeError as e:
                err = str(e)
            GLib.idle_add(done, err)

        def done(err):
            self.busy_plans.discard(plan_id)
            if err:
                self.say(err, "bad")
            self.refresh()
            return False

        threading.Thread(target=work, daemon=True).start()

    def say(self, text, kind=""):
        for c in ("good", "bad"):
            self.preview.remove_css_class(c)
        if kind:
            self.preview.add_css_class(kind)
        self.preview.set_text(text)
        self.preview.set_visible(bool(text))
        GLib.idle_add(lambda: (self.resize(), False)[1])

    def quick_changed(self, *_):
        if self.quick_timer:
            GLib.source_remove(self.quick_timer)
            self.quick_timer = 0
        text = self.quick.get_text().strip()
        if not text:
            self.say("")
            return
        self.quick_timer = GLib.timeout_add(300, self.quick_ask, text, False)

    def quick_add(self, *_):
        text = self.quick.get_text().strip()
        if text:
            if self.quick_timer:
                GLib.source_remove(self.quick_timer)
                self.quick_timer = 0
            self.quick_ask(text, True)

    def quick_ask(self, text, create):
        self.quick_timer = 0
        if self.demo:
            self.say("Made-up events: nothing is added.", "bad")
            return False
        self.quick_serial += 1
        serial = self.quick_serial

        def work():
            try:
                r, err = self.client.quick(text, create), None
            except RuntimeError as e:
                r, err = None, str(e)
            GLib.idle_add(done, r, err)

        def done(r, err):
            if serial != self.quick_serial:
                return False  # an older answer: what was typed since has its own
            if err:
                self.say(err, "bad")
            elif r.get("problem"):
                self.say(r["problem"], "bad")
            elif create and r.get("event"):
                self.quick.set_text("")
                self.say(f"Added · {M.draft_text(r.get('draft'))}", "good")
                self.refresh()
            else:
                self.say(f"{M.draft_text(r.get('draft'))}   ·   Enter adds it")
            return False

        threading.Thread(target=work, daemon=True).start()
        return False

    def open_web(self, where):
        self.open_url(f"{self.url}/{where}")

    def open_url(self, url):
        """Hands a URL to the browser and closes. The browser gets the panel's environment without what is the panel's
        own (model.child_env), and what the opener says goes to the journal: a failure here used to be silent."""
        opener = os.environ.get("DITHER_CALENDAR_PANEL_OPENER") or "xdg-open"  # tests put a stand-in here
        cmd = M.open_command(url, opener, bool(shutil.which("systemd-run")))
        log("open", url)
        try:
            p = subprocess.Popen(cmd, env=M.child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        except OSError as e:
            log("could not start", cmd[0], "-", e)
            return
        self.close()

        def work():
            try:
                # A browser already running takes the address and the opener returns; the first window of a browser
                # stays for as long as it is open, which is not a failure either.
                _, err = p.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                return
            if p.returncode:
                log("the opener failed with", p.returncode, "-", (err or "").strip()[-400:])

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ shown and hidden

    def toggle(self):
        if self.window.get_visible():
            self.close()
            return
        time.tzset()
        self.focused_once = False
        self.show_earlier = False
        self.today = date.today()
        self.selected = self.today
        self.view = (self.today.year, self.today.month)
        self.derive()
        self.render()
        self.place()
        self.window.present()
        self.month.grab_focus()
        self.refresh()
        self.ticks = 0
        self.tick_timer = self.tick_timer or GLib.timeout_add_seconds(20, self.tick)
        if self.pick_day:
            self.pick(self.pick_day)
        if self.type_words:
            self.quick.set_text(self.type_words)

    def tick(self):
        if not self.window.get_visible():
            self.tick_timer = 0
            return False
        self.ticks += 1
        if self.ticks % 6 == 0:
            self.refresh()  # every two minutes
        else:
            self.render()  # countdowns move on
        return True

    def close(self, *_):
        self.window.set_visible(False)
        if self.tick_timer:
            GLib.source_remove(self.tick_timer)
            self.tick_timer = 0
        if self.standalone:
            self.quit()
        return True

    def focus_changed(self, window, *_):
        if not window.get_visible() or self.keep_open:
            return
        if window.is_active():
            self.focused_once = True
        elif self.focused_once:
            GLib.timeout_add(120, lambda: (window.get_visible() and not window.is_active() and self.close()) and False)


def main(argv):
    def flag(name):
        return name in argv

    def opt(name, env, default):
        if name in argv and argv.index(name) + 1 < len(argv):
            return argv[argv.index(name) + 1]
        return os.environ.get(env, default)

    try:
        offset = int(opt("--offset", "DITHER_CALENDAR_PANEL_OFFSET", "0"))
    except ValueError:
        offset = 0
    app = Panel(opt("--url", "DITHER_CALENDAR_URL", M.DEFAULT_URL), holocene=not flag("--gregorian"), offset=offset,
                standalone=flag("--standalone") or flag("--demo"), demo=flag("--demo"), keep_open=flag("--keep-open"),
                type_words=opt("--type", "", ""), pick_day=date.fromisoformat(opt("--pick", "", "")) if opt("--pick", "", "") else None)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
