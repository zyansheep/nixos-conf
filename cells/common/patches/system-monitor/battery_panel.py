#!/usr/bin/env python3
"""Layer-shell battery panel: history timeline, sleep drain, what-if runtime and
power experiments, drawn from the waybar-monitor power log (see report.py)."""
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")  # compute_bounds() returns a Graphene.Rect.
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as GtkLayerShell  # noqa: E402

import eta  # noqa: E402
import experiment  # noqa: E402
import qol  # noqa: E402
import report  # noqa: E402

# Categorical slots in validated order (dark surface #222226): adjacent stack
# pairs clear CVD ΔE 8.4 / normal 19.3. Neutrals carry the always-on baseline.
COLORS = {
    'Processor baseline': '#707076', 'Rest of system': '#aaaab0',
    'Browser': '#3987e5', 'Builds & EDA': '#d95926', 'Coding tools': '#199e70', 'Display': '#c98500',
    'Chat & media': '#d55181', 'Other apps': '#008300', 'Desktop & system': '#9085e9',
}
STACK = list(COLORS)
SURFACE, CARD = (0x22 / 255, 0x22 / 255, 0x26 / 255), (0x34 / 255, 0x34 / 255, 0x37 / 255)
INK, MUTED, GRID = (1, 1, 1), (1, 1, 1, .68), (1, 1, 1, .08)
RANGES = {'6 h': 6 * 3600, 'Day': 24 * 3600, 'Week': 7 * 24 * 3600}
FREEZES = ('freeze', 'freeze-apps')
BATTERY_HOURS = [1, 2, 4, 8, 24]  # Experiment length choices, in hours of battery time.
# How to say a setting is in its (power-saving, normal) state.
PHRASES = {'aspm': ('ASPM on', 'ASPM off'), 'apst': ('APST on', 'APST off'),
           'wifi-ps': ('Wi-Fi power saving on', 'Wi-Fi power saving off'), 'abm': ('ABM on', 'ABM off'),
           'refresh': ('lower refresh rate', 'full refresh rate'), 'boost': ('CPU boost off', 'CPU boost on'),
           'profile': ('power-saver profile', 'balanced profile')}
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / 'waybar-monitor'


def rgb(color, alpha=1.0):
    color = color.lstrip('#')
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (alpha,)


def text(cr, string, x, y, size=9, color=MUTED, bold=False, align='left', width=None):
    layout = PangoCairo.create_layout(cr)
    font = Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}")
    layout.set_font_description(font)
    layout.set_text(string, -1)
    if width:
        layout.set_width(int(width * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    w, h = layout.get_pixel_size()
    if align == 'right':
        x -= w
    elif align == 'center':
        x -= w / 2
    cr.set_source_rgba(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return w, h


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def tooltip(cr, lines, x, y, width, height):
    """Values lead, labels follow; series keyed by a short stroke of their color."""
    pad, line_h = 8, 16
    w = 230
    h = pad * 2 + line_h * len(lines)
    tx = x + 14 if x + 14 + w < width else x - 14 - w
    ty = min(max(4, y - h / 2), height - h - 4)
    rounded(cr, tx, ty, w, h, 8)
    cr.set_source_rgba(*CARD, .97)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, .12)
    cr.set_line_width(1)
    cr.stroke()
    for i, (value, label, color) in enumerate(lines):
        yy = ty + pad + i * line_h
        if color:
            cr.set_source_rgba(*rgb(color))
            cr.set_line_width(3)
            cr.move_to(tx + pad, yy + 8)
            cr.line_to(tx + pad + 10, yy + 8)
            cr.stroke()
        offset = tx + pad + (16 if color else 0)
        vw, _ = text(cr, value, offset, yy, 9, INK, bold=True)
        text(cr, label, offset + vw + 6, yy, 9, MUTED, width=w - (offset - tx) - vw - 6 - pad)


def nice_step(top, ticks=4):
    raw = max(top, 1e-6) / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)


def clock(t, with_day=False):
    return time.strftime('%a %H:%M' if with_day else '%H:%M', time.localtime(t))


def time_ticks(start, end):
    """Axis ticks on local-clock boundaries: hourly, every 4 h, or daily."""
    span = end - start
    hours = 1 if span <= 6 * 3600 else 4 if span <= 86400 else 24
    day = time.localtime(start)
    midnight = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    ticks, k = [], 0
    while True:
        # mktime normalizes overflowing hours, so DST days still land on local boundaries.
        t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, k * hours, 0, 0, 0, 0, -1))
        if t >= end:
            return ticks
        if t >= start:
            ticks.append((t, time.strftime('%a %-d' if hours == 24 else '%H:%M', time.localtime(t))))
        k += 1


def wrapped(**props):
    """A wrapping label. GTK4 reports a wrapping label's whole text as its natural
    width; the cap keeps the layer surface at the card's width."""
    return Gtk.Label(**{'xalign': 0, 'wrap': True, 'max_width_chars': 100, 'width_chars': 20, **props})


def duration(hours):
    return f'{int(hours)} h {round(hours % 1 * 60):02d} m' if hours >= 1 else f'{round(hours * 60)} min'


class Chart(Gtk.DrawingArea):
    def __init__(self, height):
        super().__init__(hexpand=True, content_height=height)
        self.set_draw_func(self.draw)
        self.hover = None
        motion = Gtk.EventControllerMotion()
        motion.connect('motion', lambda _c, x, y: self.pointer(x, y))
        motion.connect('leave', lambda _c: self.pointer(None, None))
        self.add_controller(motion)
        self.loading = False

    def pointer(self, x, y):
        self.hover = None if x is None else (x, y)
        self.queue_draw()

    def draw(self, area, cr, width, height):
        cr.push_group()
        self.render(cr, width, height)
        cr.pop_group_to_source()
        cr.paint_with_alpha(.45 if self.loading else 1)

    def render(self, cr, width, height):
        raise NotImplementedError


class TimelineChart(Chart):
    """Stacked power by group (top) and battery % (strip below), one crosshair."""
    LEFT, STRIP = 44, 64

    def __init__(self):
        super().__init__(330)
        self.points, self.span, self.sleeps = [], (0, 1), []

    def set_data(self, points, span, sleeps):
        self.points, self.span, self.sleeps = points, span, sleeps
        self.queue_draw()

    def columns(self, width):
        start, end = self.span
        plot = width - self.LEFT - 8
        count = max(1, int(plot // 2))
        seconds = (end - start) / count
        bins = [None] * count
        for p in self.points:
            i = int((p['t'] - start) / seconds)
            if 0 <= i < count:
                bins[i] = bins[i] or []
                bins[i].append(p)
        merged = []
        for points in bins:
            if not points:
                merged.append(None)
                continue
            groups = {}
            for p in points:
                for name, watts in p['groups'].items():
                    groups[name] = groups.get(name, 0) + watts / len(points)
            merged.append({'groups': groups, 'points': points, 'battery': sum(p['st'] == 'D' for p in points) * 2 > len(points),
                           'pct': next((p['pct'] for p in reversed(points) if p.get('pct') is not None), None)})
        return merged, plot / count

    def render(self, cr, width, height):
        start, end = self.span
        top_h = height - self.STRIP - 30
        columns, column_w = self.columns(width)
        totals = [sum(c['groups'].values()) for c in columns if c]
        y_max = max(totals or [10]) * 1.08
        step = nice_step(y_max)
        y_max = math.ceil(y_max / step) * step
        sx = lambda t: self.LEFT + (t - start) / (end - start) * (width - self.LEFT - 8)
        sy = lambda w: 6 + top_h - w / y_max * top_h
        # Grid and y labels (recessive).
        cr.set_line_width(1)
        value = 0
        while value <= y_max + 1e-6:
            cr.set_source_rgba(*GRID)
            cr.move_to(self.LEFT, sy(value) + .5)
            cr.line_to(width - 8, sy(value) + .5)
            cr.stroke()
            text(cr, f'{value:g} W', self.LEFT - 6, sy(value) - 7, 8, MUTED, align='right')
            value += step
        for sleep in self.sleeps:
            x0, x1 = max(self.LEFT, sx(sleep['start'])), min(width - 8, sx(sleep['end']))
            if x1 > x0:
                cr.set_source_rgba(1, 1, 1, .035)
                cr.rectangle(x0, 6, x1 - x0, top_h)
                cr.fill()
                if x1 - x0 > 40:
                    text(cr, 'asleep', (x0 + x1) / 2, 10, 8, MUTED, align='center')
        for i, column in enumerate(columns):
            if not column:
                continue
            x, base = self.LEFT + i * column_w, 0.0
            alpha = 1.0 if column['battery'] else .4  # On AC: chip power only.
            for name in STACK:
                watts = column['groups'].get(name, 0)
                if watts <= 0:
                    continue
                cr.set_source_rgba(*rgb(COLORS[name], alpha))
                cr.rectangle(x, sy(base + watts), column_w + .3, sy(base) - sy(base + watts))
                cr.fill()
                base += watts
        # Battery % strip: separate scale, never a second y-axis on the stack.
        strip_top = 6 + top_h + 18
        text(cr, 'Battery %', self.LEFT + 4, strip_top - 14, 8, MUTED)
        for value in (0, 50, 100):
            y = strip_top + self.STRIP - value / 100 * self.STRIP
            cr.set_source_rgba(*GRID)
            cr.move_to(self.LEFT, y + .5)
            cr.line_to(width - 8, y + .5)
            cr.stroke()
            if value:
                text(cr, f'{value}', self.LEFT - 6, y - 7, 8, MUTED, align='right')
        cr.set_source_rgba(1, 1, 1, .85)
        cr.set_line_width(2)
        previous = None
        for i, column in enumerate(columns):
            if not column or column['pct'] is None:
                previous = None
                continue
            x, y = self.LEFT + (i + .5) * column_w, strip_top + self.STRIP - column['pct'] / 100 * self.STRIP
            if previous is None:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
            previous = (x, y)
        cr.stroke()
        # Time axis.
        span = end - start
        for t, label in time_ticks(start, end):
            x = sx(t)
            text(cr, label, x, height - 16, 8, MUTED, align='center')
            cr.set_source_rgba(*GRID)
            cr.move_to(x + .5, 6)
            cr.line_to(x + .5, 6 + top_h)
            cr.stroke()
        if not self.points:
            text(cr, 'No power-log data in this period', width / 2, top_h / 2, 11, MUTED, align='center')
            return
        if self.hover:
            hx, hy = self.hover
            i = int((hx - self.LEFT) / column_w)
            if 0 <= i < len(columns) and columns[i]:
                column = columns[i]
                x = self.LEFT + (i + .5) * column_w
                cr.set_source_rgba(1, 1, 1, .5)
                cr.set_line_width(1)
                cr.move_to(x + .5, 6)
                cr.line_to(x + .5, strip_top + self.STRIP)
                cr.stroke()
                points = column['points']
                t0, t1 = points[0]['t'], points[-1]['t'] + 60
                total = sum(column['groups'].values())
                lines = [(f'{total:.1f} W', ('on battery · ' if column['battery'] else 'on AC · chip only · ')
                          + f"{clock(t0, span > 86400)}–{clock(t1)}", None)]
                for name in sorted(column['groups'], key=lambda n: -column['groups'][n]):
                    if column['groups'][name] >= .05:
                        lines.append((f"{column['groups'][name]:.1f} W", name, COLORS[name]))
                apps = {}
                for p in points:
                    for name, watts in p['apps'].items():
                        apps[name] = apps.get(name, 0) + watts / len(points)
                top = sorted(apps.items(), key=lambda kv: -kv[1])[:3]
                if top:
                    lines.append(('Top apps', '', None))
                    lines += [(f'{w:.2f} W', name, None) for name, w in top]
                if column['pct'] is not None:
                    lines.append((f"{column['pct']:.0f}%", 'battery', None))
                tooltip(cr, lines, hx, hy, width, height)


class SleepChart(Chart):
    LEFT = 44

    def __init__(self):
        super().__init__(190)
        self.sleeps, self.span, self.median = [], (0, 1), None

    def set_data(self, sleeps, span, median):
        self.sleeps, self.span, self.median = sleeps, span, median
        self.queue_draw()

    def render(self, cr, width, height):
        start, end = self.span
        plot_h = height - 40
        top = max([s['pct_per_hour'] for s in self.sleeps] + [self.median or 0, 3]) * 1.15
        step = nice_step(top)
        top = math.ceil(top / step) * step
        sx = lambda t: self.LEFT + (t - start) / (end - start) * (width - self.LEFT - 12)
        sy = lambda v: 8 + plot_h - v / top * plot_h
        value = 0
        while value <= top + 1e-6:
            cr.set_source_rgba(*GRID)
            cr.set_line_width(1)
            cr.move_to(self.LEFT, sy(value) + .5)
            cr.line_to(width - 12, sy(value) + .5)
            cr.stroke()
            text(cr, f'{value:g} %/h', self.LEFT - 6, sy(value) - 7, 8, MUTED, align='right')
            value += step
        if self.median is not None:
            cr.set_source_rgba(1, 1, 1, .45)
            cr.set_dash([4, 4])
            cr.move_to(self.LEFT, sy(self.median))
            cr.line_to(width - 12, sy(self.median))
            cr.stroke()
            cr.set_dash([])
            text(cr, f'median {self.median:.1f} %/h (all history)', self.LEFT + 4, sy(self.median) - 15, 8, MUTED)
        if not self.sleeps:
            text(cr, 'No suspends in this period', width / 2, plot_h / 2, 11, MUTED, align='center')
        nearest, best = None, 24
        for s in self.sleeps:
            x, y = sx((s['start'] + s['end']) / 2), sy(s['pct_per_hour'])
            radius = 4 + min(6, s['hours'])
            cr.arc(x, y, radius + 2, 0, 2 * math.pi)
            cr.set_source_rgb(*SURFACE)
            cr.fill()  # 2px surface ring keeps overlapping dots distinct.
            cr.arc(x, y, radius, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb(COLORS['Browser']))
            cr.fill()
            if self.hover and math.hypot(self.hover[0] - x, self.hover[1] - y) < best:
                nearest, best = s, math.hypot(self.hover[0] - x, self.hover[1] - y)
        span = end - start
        for t, label in time_ticks(start, end):
            text(cr, label, sx(t), height - 18, 8, MUTED, align='center')
        if nearest:
            s = nearest
            tooltip(cr, [(f"{s['pct_per_hour']:.1f} %/h", f"{s['watts']:.2f} W while asleep", COLORS['Browser']),
                         (duration(s['hours']), f"{clock(s['start'], True)} → {clock(s['end'], True)}", None),
                         (f"{s['pct_before']:.0f}% → {s['pct_after']:.0f}%", 'battery', None)],
                    *self.hover, width, height)


class WhatIfChart(Chart):
    ROW = 28

    def __init__(self, on_activate):
        super().__init__(200)
        self.rows, self.on_activate = [], on_activate
        click = Gtk.GestureClick()
        click.connect('released', self.clicked)
        self.add_controller(click)

    def set_data(self, rows):
        self.rows = rows
        self.set_content_height(max(120, 30 + self.ROW * len(rows)))
        self.queue_draw()

    def row_at(self, y):
        i = int((y - 24) // self.ROW)
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def clicked(self, _gesture, _n, _x, y):
        row = self.row_at(y)
        if row and row.get('pending'):
            self.on_activate(row)

    def render(self, cr, width, height):
        label_w, value_w = 230, 150
        plot_x0, plot_x1 = label_w + 8, width - value_w
        values = [v for r in self.rows if not r.get('pending') for v in (r['low'], r['high'], r['minutes'])
                  if v is not None and math.isfinite(v)]
        low, high = min(values + [0]), max(values + [10])
        step = nice_step(high - low, 5)
        low, high = math.floor(low / step) * step, math.ceil(high / step) * step
        sx = lambda m: plot_x0 + (m - low) / (high - low) * (plot_x1 - plot_x0)
        value = low
        while value <= high + 1e-6:
            cr.set_source_rgba(*GRID)
            cr.set_line_width(1)
            cr.move_to(sx(value) + .5, 18)
            cr.line_to(sx(value) + .5, height - 4)
            cr.stroke()
            text(cr, f'{value:+g} min' if value else '0', sx(value), 2, 8, MUTED, align='center')
            value += step
        cr.set_source_rgba(1, 1, 1, .35)
        cr.move_to(sx(0) + .5, 18)
        cr.line_to(sx(0) + .5, height - 4)
        cr.stroke()
        hovered = self.row_at(self.hover[1]) if self.hover else None
        for i, row in enumerate(self.rows):
            y = 24 + i * self.ROW
            text(cr, row['name'], 4, y + 6, 9, INK if row is hovered else MUTED, width=label_w - 8)
            if row.get('pending'):
                text(cr, row['pending'], plot_x0, y + 6, 9, (*rgb(COLORS['Display'])[:3], .9))
                continue
            measured = row['evidence'] in ('experiment', 'boot')
            color = COLORS['Coding tools'] if measured else COLORS['Browser']
            x0, x1 = sorted((sx(0), sx(row['minutes'])))
            rounded(cr, x0, y + 6, max(2, x1 - x0), self.ROW - 12, 3)
            if measured:
                cr.set_source_rgba(*rgb(color))
                cr.fill()
            else:  # Model estimates: outlined, never mistaken for measurements.
                cr.set_source_rgba(*rgb(color, .28))
                cr.fill_preserve()
                cr.set_source_rgba(*rgb(color))
                cr.set_line_width(1.5)
                cr.stroke()
            if row.get('low') is not None and math.isfinite(row['low']) and math.isfinite(row['high']):
                cr.set_source_rgba(1, 1, 1, .85)
                cr.set_line_width(1.5)
                cy = y + self.ROW / 2
                cr.move_to(sx(row['low']), cy)
                cr.line_to(sx(row['high']), cy)
                for edge in (row['low'], row['high']):
                    cr.move_to(sx(edge), cy - 4)
                    cr.line_to(sx(edge), cy + 4)
                cr.stroke()
            interval = (f" ({row['low']:+.0f} to {row['high']:+.0f})"
                        if row.get('low') is not None and math.isfinite(row['low']) else '')
            text(cr, f"{row['minutes']:+.0f} min{interval}", width - 4, y + 6, 9, INK, align='right')
        if hovered and self.hover:
            lines = self.details(hovered)
            tooltip(cr, lines, *self.hover, width, height)

    @staticmethod
    def details(row):
        if row.get('pending'):
            return [('Not measured', row['name'], None), ('Click', 'to open Experiments', None)]
        lines = [(f"{row['minutes']:+.1f} min", 'per full charge', None),
                 (f"{row['watts']:+.2f} W", 'average saving', None)]
        if row['evidence'] == 'experiment':
            lines.append((f"{row['pairs']} pairs", 'of randomized A/B blocks', COLORS['Coding tools']))
        elif row['evidence'] == 'boot':
            lines.append((f"{row['boots']['on']}+{row['boots']['off']} boots", 'with / without, load-adjusted',
                          COLORS['Coding tools']))
        else:
            lines.append(('Model', 'removes activity; ×chip cost', COLORS['Browser']))
        if row.get('low') is not None:
            lines.append((f"{row['low']:+.0f} … {row['high']:+.0f}", '90% interval (min)', None))
        return lines


class History:
    """The last 30 days of minutes and events. While the panel is open, a reload
    (range switch, paging, the minute tick) fetches only minutes from the newest
    one on, which may still have been filling."""

    def __init__(self):
        self.lock = threading.Lock()
        self.minutes, self.events, self.since = [], [], None

    def get(self):
        with self.lock:
            now = time.time()
            start = now - 30 * 86400
            if self.since is None:
                self.minutes, self.events = report.load_range(start, now + 60)
            else:
                minutes, events = report.load_range(self.since, now + 60)
                known = {(e.get('event'), e.get('t1')) for e in self.events}
                self.minutes = [m for m in self.minutes if start <= m['t'] < self.since] + minutes
                self.events = ([e for e in self.events if e.get('t1', 0) >= start]
                               + [e for e in events if (e.get('event'), e.get('t1')) not in known])
            self.since = self.minutes[-1]['t'] if self.minutes else None
            return list(self.minutes), list(self.events)


def gather(start, end, history=None):
    """Everything the panel shows for one period (runs off the UI thread)."""
    history, events = (history or History()).get()
    rows_all = report.battery_rows(history)
    # battery-eta trains every model; fit here only if its models.json is missing or stale.
    shared = eta.load_models()
    model = shared['attribution'] if shared else report.fit_rows(rows_all)
    minutes = [m for m in history if start <= m['t'] < end]
    if start < time.time() - 30 * 86400:
        minutes, events_old = report.load_range(start, end)
        events = events_old + events
    period_events = [e for e in events if start - 86400 <= e.get('t1', 0) < end + 86400]
    sleeps = [s for s in report.sleep_drain(period_events) if s['end'] > start and s['start'] < end]
    rows = report.battery_rows(minutes)
    scope = 'this period'
    if len(rows) < 30:
        rows, scope = rows_all, 'all history (too little battery time in this period)'
    energy = report.full_energy(history)
    estimates, info = report.what_if(rows, energy, model=model, train_rows=rows_all) if rows else ([], None)
    power = info['power'] if info else (sum(m['bat'] for m in rows_all) / len(rows_all) if rows_all else None)
    pool = experiment.pool()
    # App freezing runs only while locked, so it stays a separate paired A/B analysis.
    freezes = [e for e in report.experiment_effects(history, energy, power)
               if e['experiment'] in FREEZES] if power else []
    return dict(points=report.timeline(minutes, model), span=(start, end), sleeps=sleeps,
                sleeps_all=report.sleep_drain(events), estimates=estimates, info=info, scope=scope,
                energy=energy, power=power, levers=report.lever_effects(history, energy, power), effects=freezes,
                pool=pool, plan=report.plan(history, events, pool) if pool else None, goal=experiment.goal(),
                psr=report.boot_effect(history, energy, power), psr_arm=experiment.psr_arm(),
                catalog=[experiment.describe(spec) for spec in experiment.catalog().values()],
                units=experiment.freezable_units()[:12], model=model,
                trained=shared['fitted'] if shared else None)


def model_rows(result):
    """Model estimates (apps, display) for the chart, and their caption."""
    info = result['info']
    if not info:
        return [], 'Not enough battery time logged yet for model estimates.'
    note = ("Model estimates: the battery model removes an app's or group's activity, or dims the display, and "
            "assumes nothing else changes (outlined bars; whiskers are 90% intervals)."
            + (f" Models trained {time.strftime('%H:%M', time.localtime(result['trained']))} by battery-eta."
               if result.get('trained') else ' Models fitted here (battery-eta has not trained recently).'))
    return result['estimates'], note


def render(directory, range_name='Day', offset=0):
    """Draw the three charts to PNGs without opening a window (`--render DIR`)."""
    import cairo
    length = RANGES[range_name]
    today = time.localtime()
    midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    end = (time.time() if range_name == '6 h' else midnight + 86400) + offset * length
    result = gather(end - length, end)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows, _ = model_rows(result)
    drains = sorted(s['pct_per_hour'] for s in result['sleeps_all'])
    charts = {'timeline': (TimelineChart(), (result['points'], result['span'], result['sleeps']), 330),
              'sleep': (SleepChart(), (result['sleeps'], result['span'], drains[len(drains) // 2] if drains else None), 300),
              'whatif': (WhatIfChart(lambda _row: None), (rows,), 30 + WhatIfChart.ROW * len(rows))}
    for name, (chart, data, height) in charts.items():
        chart.set_data(*data)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 880, max(height, 120))
        cr = cairo.Context(surface)
        cr.set_source_rgb(*SURFACE)
        cr.paint()
        chart.render(cr, 880, max(height, 120))
        surface.write_to_png(str(directory / f'{name}.png'))
        print(directory / f'{name}.png')


class GainBar(Gtk.DrawingArea):
    """One saving (minutes per charge) and its 90% interval, on a scale shared down its column."""

    def __init__(self):
        super().__init__(content_width=190, content_height=18, valign=Gtk.Align.CENTER)
        self.gain, self.scale = None, (-10, 10)
        self.set_draw_func(self.draw)

    def set_data(self, gain, scale):
        self.gain, self.scale = gain, scale
        self.queue_draw()

    def draw(self, _area, cr, width, height):
        low, high = self.scale
        sx = lambda m: 1 + (min(max(m, low), high) - low) / (high - low) * (width - 2)
        cr.set_source_rgba(1, 1, 1, .35)
        cr.set_line_width(1)
        cr.move_to(round(sx(0)) + .5, 1)
        cr.line_to(round(sx(0)) + .5, height - 1)
        cr.stroke()
        if not self.gain or self.gain.get('minutes') is None or not math.isfinite(self.gain['minutes']):
            return
        color = COLORS['Coding tools'] if self.gain['minutes'] >= 0 else COLORS['Builds & EDA']
        x0, x1 = sorted((sx(0), sx(self.gain['minutes'])))
        rounded(cr, x0, 4, max(2, x1 - x0), height - 8, 3)
        cr.set_source_rgba(*rgb(color))
        cr.fill()
        if self.gain.get('low') is not None and math.isfinite(self.gain['low']) and math.isfinite(self.gain['high']):
            cr.set_source_rgba(1, 1, 1, .85)
            cr.set_line_width(1.5)
            cy = height / 2
            cr.move_to(sx(self.gain['low']), cy)
            cr.line_to(sx(self.gain['high']), cy)
            for edge in (self.gain['low'], self.gain['high']):
                cr.move_to(sx(edge), cy - 4)
                cr.line_to(sx(edge), cy + 4)
            cr.stroke()


def gain_text(gain):
    if not gain or gain.get('minutes') is None:
        return ''
    if not math.isfinite(gain['minutes']):
        return 'unbounded'
    interval = (f" ({gain['low']:+.0f} … {gain['high']:+.0f})"
                if gain.get('low') is not None and math.isfinite(gain['low']) and math.isfinite(gain['high']) else '')
    return f"{gain['minutes']:+.0f} min{interval}"


def shared_scale(gains):
    values = [v for g in gains if g for v in (g.get('minutes'), g.get('low'), g.get('high'))
              if v is not None and math.isfinite(v)]
    low, high = min(values + [-5]), max(values + [15])
    step = nice_step(high - low, 4)
    return math.floor(low / step) * step, math.ceil(high / step) * step


class BatteryPanel(Adw.Application):
    def __init__(self):
        super().__init__(application_id='org.zyansheep.BatteryPanel', flags=Gio.ApplicationFlags.IS_SERVICE)
        self.window = None
        self.range_name, self.offset = 'Day', 0
        self.focused_once = False
        self.generation = 0
        self.loaded = None
        self.history = History()
        self.poll = 0
        self.catalog, self.units, self.effects = [], [], []
        self.psr, self.psr_arm = None, None
        self.result, self.best, self.quality = None, None, None
        self.syncing = False  # Set while widgets are updated from state, so they don't echo it back.
        self.note = ''
        self.connect('startup', self.startup)
        self.connect('activate', self.toggle)

    # --- window ------------------------------------------------------------------
    def startup(self, *_):
        self.hold()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        action = Gio.SimpleAction.new('toggle', None)
        action.connect('activate', self.toggle)
        self.add_action(action)
        # `battery-panel <page>` opens directly on timeline/sleep/whatif/experiments.
        show = Gio.SimpleAction.new('show', GLib.VariantType.new('s'))
        show.connect('activate', self.show_page)
        self.add_action(show)
        css = Gtk.CssProvider()
        palette = Path(__file__).resolve().parents[2] / 'share/battery-panel/menu-theme.css'  # $out/libexec/waybar-monitor/
        css.load_from_data((palette.read_bytes() if palette.exists() else b'') + CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.build()
        self.window.realize()

    def build(self):
        self.window = Adw.ApplicationWindow(application=self, title='Battery')
        self.window.connect('close-request', self.close)
        self.window.connect('notify::is-active', self.focus_changed)
        self.window.set_decorated(False)
        GtkLayerShell.init_for_window(self.window)
        GtkLayerShell.set_namespace(self.window, 'battery-panel')
        GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
        # BATTERY_PANEL_PASSIVE=1 shows the panel without ever taking keyboard focus.
        keyboard = (GtkLayerShell.KeyboardMode.NONE if os.environ.get('BATTERY_PANEL_PASSIVE')
                    else GtkLayerShell.KeyboardMode.ON_DEMAND)
        GtkLayerShell.set_keyboard_mode(self.window, keyboard)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, 32)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key)
        self.window.add_controller(keys)
        # Close on focus loss once the panel has had focus: niri focuses an
        # on-demand panel when it opens, so a click on another window dismisses
        # it, like Audio and Wi-Fi. (A freshly mapped surface can report
        # inactive before niri focuses it, hence waiting for the first focus.)
        # The pointer entering also counts, for BATTERY_PANEL_PASSIVE.
        engaged = Gtk.EventControllerMotion()
        engaged.connect('enter', lambda *_: setattr(self, 'focused_once', True))
        self.window.add_controller(engaged)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.set_size_request(880, -1)
        # Wrapping labels and the legend report their one-line width as natural
        # width; the clamp keeps the layer surface at the intended size.
        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=880, child=card)
        outer = Gtk.Box()
        outer.set_name('battery-card')
        outer.append(clamp)
        # GTK's tooltips wait half a second (fixed in GTK 4); hints show at once.
        self.hint_anchor, self.hint_label = outer, wrapped(max_width_chars=60)
        self.hint_popover = Gtk.Popover(autohide=False, can_focus=False, position=Gtk.PositionType.TOP,
                                        child=self.hint_label)
        self.hint_popover.add_css_class('hint')
        self.hint_popover.set_parent(outer)
        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label='Battery', xalign=0)
        title.add_css_class('title-2')
        header.append(title)
        self.status = Gtk.Label(xalign=0, hexpand=True)
        self.status.add_css_class('muted')
        header.append(self.status)
        close = Gtk.Button.new_from_icon_name('window-close-symbolic')
        close.add_css_class('flat')
        close.update_property([Gtk.AccessibleProperty.LABEL], ['Close battery panel'])
        close.connect('clicked', self.close)
        header.append(close)
        card.append(header)

        # One filter row above everything it scopes.
        controls = Gtk.Box(spacing=6)
        group = None
        for name in RANGES:
            button = Gtk.ToggleButton(label=name)
            button.set_group(group)
            group = group or button
            button.set_active(name == self.range_name)
            button.connect('toggled', lambda b, n=name: b.get_active() and self.set_range(n))
            controls.append(button)
        controls.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        back = Gtk.Button.new_from_icon_name('go-previous-symbolic')
        back.connect('clicked', lambda _b: self.shift(-1))
        controls.append(back)
        self.period = Gtk.Label(width_chars=26)
        controls.append(self.period)
        self.forward = Gtk.Button.new_from_icon_name('go-next-symbolic')
        self.forward.connect('clicked', lambda _b: self.shift(1))
        controls.append(self.forward)
        now = Gtk.Button(label='Now')
        now.connect('clicked', lambda _b: self.shift(None))
        controls.append(now)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.summary = Gtk.Label(xalign=1, hexpand=True)
        self.summary.add_css_class('muted')
        controls.append(self.summary)
        card.append(controls)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        card.append(switcher)
        card.append(self.stack)

        self.timeline = TimelineChart()
        self.legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=9, column_spacing=12)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.timeline)
        page.append(self.legend)
        note = wrapped(label='Average watts per moment, split by the battery model. Faded '
                         'columns are on AC (chip power only); shaded bands are sleep. Hover for apps.')
        note.add_css_class('muted')
        page.append(note)
        # Sleep drain sits under the timeline it shades: one dot per suspend in this period.
        page.append(self.section('Sleep drain'))
        self.sleep = SleepChart()
        page.append(self.sleep)
        self.sleep_note = self.muted(wrapped())
        page.append(self.sleep_note)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=430, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(page)
        self.stack.add_titled(scroller, 'timeline', 'Timeline')
        self.stack.add_titled(self.build_experiments(), 'experiments', 'Experiments')
        self.window.set_content(outer)

    def build_experiments(self):
        """Automatic experiments and 👍/👎 votes, what each setting saves,
        model estimates for apps and the display, and the lock-screen and boot
        experiments."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Gtk.Box(spacing=10)
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.auto_switch.connect('state-set', self.switch_experiments)
        top.append(self.auto_switch)
        title = Gtk.Label(label='Automatic experiments', xalign=0)
        title.add_css_class('heading')
        top.append(title)
        self.banner = wrapped(hexpand=True)
        self.banner.add_css_class('banner')
        top.append(self.banner)
        self.stop_button = Gtk.Button(label='Stop', valign=Gtk.Align.CENTER)
        self.stop_button.add_css_class('destructive-action')
        self.stop_button.connect('clicked', lambda _b: self.command(['power-experiment', 'stop']))
        top.append(self.stop_button)
        page.append(top)
        votes = Gtk.Box(spacing=8)
        votes.append(Gtk.Label(label='Right now the laptop is'))
        for choice, label in (('up', 'Especially good'), ('down', 'Worse than it should be')):
            content = Gtk.Box(spacing=6)
            thumb = Gtk.Label(label=qol.GLYPHS[choice])
            thumb.add_css_class('thumb')
            content.append(thumb)
            content.append(Gtk.Label(label=label))
            button = Gtk.Button(child=content)
            button.add_css_class(f'vote-{choice}')
            button.connect('clicked', lambda _b, c=choice: self.command(['qol', c], done=None))
            votes.append(button)
        self.vote_note = self.muted(wrapped(hexpand=True))
        votes.append(self.vote_note)
        self.resume_button = Gtk.Button(label='Resume now', valign=Gtk.Align.CENTER)
        self.resume_button.connect('clicked', lambda _b: self.command(['qol', 'resume']))
        votes.append(self.resume_button)
        page.append(votes)
        self.experiments_note = self.muted(wrapped())
        page.append(self.experiments_note)

        page.append(self.section('Settings'))
        self.lever_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lever_list.add_css_class('boxed-list')
        page.append(self.lever_list)
        self.accuracy = self.muted(wrapped())
        page.append(self.accuracy)
        self.together = self.muted(wrapped())
        page.append(self.together)
        best = Gtk.Box(spacing=8)
        goals = Gtk.Box(valign=Gtk.Align.CENTER)
        goals.add_css_class('linked')
        self.goal_buttons, group = {}, None
        for name, label in (('battery', 'Battery'), ('comfort', 'Comfort')):
            button = Gtk.ToggleButton(label=label)
            button.set_group(group)
            group = group or button
            button.connect('toggled', lambda b, n=name: b.get_active() and not self.syncing and self.set_goal(n))
            goals.append(button)
            self.goal_buttons[name] = button
        best.append(goals)
        self.best_label = wrapped(hexpand=True)
        self.best_label.add_css_class('result')
        best.append(self.best_label)
        self.apply_button = Gtk.Button(label='Apply', valign=Gtk.Align.CENTER)
        self.hint(self.apply_button, 'Until reboot. ASPM and APST also revert at the next suspend (the crash '
                                     'workaround), ABM and the profile at the next plug change.')
        self.apply_button.connect('clicked', lambda _b: self.apply_best())
        best.append(self.apply_button)
        page.append(best)

        page.append(self.section('Apps and display (model estimates)'))
        self.whatif = WhatIfChart(lambda _row: None)
        page.append(self.whatif)
        self.whatif_note = self.muted(wrapped())
        page.append(self.whatif_note)

        page.append(self.section('On the lock screen and at boot'))
        length = Gtk.Box(spacing=8)
        length.append(Gtk.Label(label='Freezing experiments run for'))
        self.minutes = Gtk.DropDown.new_from_strings([f'{h} h' for h in BATTERY_HOURS])
        self.minutes.set_selected(BATTERY_HOURS.index(4))
        length.append(self.minutes)
        length.append(Gtk.Label(label='of battery time (they stop automatic experiments meanwhile).'))
        page.append(length)
        self.experiment_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.experiment_list.add_css_class('boxed-list')
        page.append(self.experiment_list)
        scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=430, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(page)
        return scroller

    @staticmethod
    def section(title):
        label = Gtk.Label(label=title, xalign=0, margin_top=6)
        label.add_css_class('heading')
        return label

    def key(self, _controller, keyval, *_):
        if keyval == Gdk.KEY_Escape:
            return self.close()
        if keyval == Gdk.KEY_Left:
            self.shift(-1)
        elif keyval == Gdk.KEY_Right:
            self.shift(1)
        return False

    def toggle(self, *_):
        if self.window.get_visible():
            self.close()
            return
        self.focused_once = False
        self.window.present()
        self.refresh_status()
        self.reload()
        self.poll = self.poll or GLib.timeout_add_seconds(2, self.tick)

    def show_page(self, _action, page):
        name = {'sleep': 'timeline', 'whatif': 'experiments', 'savings': 'experiments'}.get(page.get_string(),
                                                                                           page.get_string())
        if self.stack.get_child_by_name(name):
            self.stack.set_visible_child_name(name)
        if not self.window.get_visible():
            self.toggle()

    def hint(self, widget, text):
        motion = Gtk.EventControllerMotion()
        motion.connect('enter', lambda *_: self.show_hint(widget, text))
        motion.connect('leave', lambda *_: self.hint_popover.popdown())
        widget.add_controller(motion)

    def show_hint(self, widget, text):
        found, bounds = widget.compute_bounds(self.hint_anchor)
        if not found:
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(bounds.get_x()), int(bounds.get_y())
        rect.width, rect.height = int(bounds.get_width()), int(bounds.get_height())
        self.hint_label.set_text(text)
        self.hint_popover.set_pointing_to(rect)
        self.hint_popover.popup()

    def close(self, *_):
        self.hint_popover.popdown()
        self.window.set_visible(False)
        self.history = History()  # Free the 30 days of rows; the next open reloads them.
        if self.poll:
            GLib.source_remove(self.poll)
            self.poll = 0
        return True

    def focus_changed(self, window, *_):
        if not window.get_visible():
            return
        if window.is_active():
            self.focused_once = True
        elif self.focused_once:
            # A dropdown's popover briefly takes focus within the panel.
            GLib.timeout_add(150, lambda: (window.get_visible() and not window.is_active() and self.close()) and False)

    def tick(self):
        if not self.window.get_visible():
            self.poll = 0
            return False
        self.refresh_status()
        if self.loaded and time.time() - self.loaded > 60 and self.offset == 0:
            self.reload()
        return True

    # --- data -------------------------------------------------------------------------
    def span(self):
        length = RANGES[self.range_name]
        if self.range_name == '6 h':
            end = time.time() + self.offset * length
            return end - length, end
        today = time.localtime()
        midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
        if self.range_name == 'Day':
            start = midnight + self.offset * 86400
            return start, start + 86400
        end = midnight + 86400 + self.offset * length
        return end - length, end

    def set_range(self, name):
        self.range_name, self.offset = name, 0
        self.reload()

    def shift(self, delta):
        self.offset = 0 if delta is None else min(0, self.offset + delta)
        self.reload()

    def reload(self):
        self.generation += 1
        generation = self.generation
        start, end = self.span()
        fmt = '%a %b %-d, %H:%M' if self.range_name == '6 h' else '%a %b %-d'
        label = time.strftime(fmt, time.localtime(start))
        if self.range_name == 'Week':
            label += ' – ' + time.strftime('%a %b %-d', time.localtime(end - 1))
        self.period.set_text(label)
        self.forward.set_sensitive(self.offset < 0)
        for chart in (self.timeline, self.sleep, self.whatif):
            chart.loading = True
            chart.queue_draw()
        self.spinner.start()
        threading.Thread(target=self.load, args=(generation, start, end), daemon=True).start()

    def load(self, generation, start, end):
        try:
            result = gather(start, end, self.history)
        except Exception as error:  # noqa: BLE001 - surfaced in the panel
            result = {'error': f'{type(error).__name__}: {error}'}
        GLib.idle_add(self.apply, generation, result)

    def apply(self, generation, result):
        if generation != self.generation:
            return False
        self.spinner.stop()
        self.loaded = time.time()
        for chart in (self.timeline, self.sleep, self.whatif):
            chart.loading = False
        if 'error' in result:
            self.summary.set_text(result['error'])
            return False
        points = result['points']
        self.timeline.set_data(points, result['span'], result['sleeps'])
        self.fill_legend(points)
        battery = [p for p in points if p['st'] == 'D' and p['bat']]
        if battery:
            mean = sum(p['bat'] for p in battery) / len(battery)
            self.summary.set_text(f'{duration(len(battery) / 60)} on battery · {mean:.1f} W average')
        else:
            self.summary.set_text('No battery time in this period')
        drains = sorted(s['pct_per_hour'] for s in result['sleeps_all'])
        median = drains[len(drains) // 2] if drains else None
        self.sleep.set_data(result['sleeps'], result['span'], median)
        if drains:
            watts = sorted(s['watts'] for s in result['sleeps_all'])[len(drains) // 2]
            self.sleep_note.set_text(f'{len(drains)} suspends logged; median drain {median:.1f} %/h ({watts:.2f} W) — '
                                     f'about {100 / median:.0f} h from full to empty asleep. Dot size is sleep length.'
                                     if median > 0 else f'{len(drains)} suspends logged.')
        else:
            self.sleep_note.set_text('No suspends logged yet.')
        self.effects = result['effects']
        self.catalog, self.units = result['catalog'], result['units']
        self.psr, self.psr_arm = result.get('psr'), result.get('psr_arm')
        self.result = result
        self.fill_experiments()
        return False

    def fill_legend(self, points):
        while (child := self.legend.get_first_child()) is not None:
            self.legend.remove(child)
        present = {name for p in points for name, w in p['groups'].items() if w > .05}
        for name in STACK:
            if name not in present:
                continue
            box = Gtk.Box(spacing=5)
            swatch = Gtk.DrawingArea(content_width=12, content_height=12, valign=Gtk.Align.CENTER)
            swatch.set_draw_func(lambda _a, cr, w, h, c=COLORS[name]: (cr.set_source_rgba(*rgb(c)),
                                                                     rounded(cr, 0, 0, w, h, 3), cr.fill()))
            box.append(swatch)
            label = Gtk.Label(label=name)
            label.add_css_class('muted')
            box.append(label)
            self.legend.append(box)

    # --- experiments ------------------------------------------------------------------------
    def experiment_state(self):
        try:
            return json.loads((RUNTIME / 'experiment.json').read_text())
        except (OSError, ValueError):
            return {}

    def settings(self):
        return [c for c in self.catalog if c['name'] in report.SETTING]

    def manual_active(self):
        state = self.experiment_state()
        return state.get('mode') != 'auto' and state.get('status') in ('running', 'paused')

    def fill_experiments(self):
        self.hint_popover.popdown()  # Its widget may be about to go.
        result = self.result
        levers, plan = result['levers'], result.get('plan')
        effects = {e['experiment']: e for e in levers['effects']}
        info = result['info']
        draw = (f"Typical draw {result['power']:.1f} W ({result['scope'] if info else 'all history'}) → "
                f"{result['energy'] / result['power']:.1f} h per full charge ({result['energy']:.0f} Wh). "
                if result.get('power') and result.get('energy') else '')
        self.experiments_note.set_text(
            draw + 'While on, every 4-minute block on battery sets the included settings to the combination the '
            'battery model expects to learn most from — among those unlikely to bother you, or any while the screen '
            'is locked — and learns from your 👎 which settings to avoid and from your 👍 which are worth keeping '
            '(the Comfort goal). Gains are minutes '
            'per full charge (90% intervals), each for that setting alone with the others as they are on battery.')
        while (row := self.lever_list.get_first_child()) is not None:
            self.lever_list.remove(row)
        scale = shared_scale(list(effects.values()))
        pool = set(result.get('pool') or [])
        quality = plan['qol'] if plan else report.qol_model([], [])
        for spec in self.settings():
            self.lever_list.append(self.lever_row(spec, effects.get(spec['name']),
                                                  levers['progress'].get(spec['name']), scale,
                                                  spec['name'] in pool, quality))
        if plan and plan['accuracy'] is not None:
            least = ', '.join(PHRASES.get(n, (n, n))[0] for n, on in plan['least_certain'].items() if on) or 'all off'
            self.accuracy.set_text(f"Battery model: for the {plan['allowed']} of {plan['combinations']} combinations "
                                   f"allowed now, predictions are within ±{plan['accuracy']:.1f} W on average (90%); "
                                   f"least certain: {least}. That is where the next blocks look.")
        else:
            self.accuracy.set_text('Include settings to experiment with.')
        found = [i for i in levers['interactions'] if i['watts_low'] > 0 or i['watts_high'] < 0]
        if found:
            self.together.set_text('Together: ' + '; '.join(
                f"{PHRASES.get(a, (a,))[0]} with {PHRASES.get(b, (b,))[0]} saves {i['watts']:+.2f} W "
                f"({i['watts_low']:+.2f} … {i['watts_high']:+.2f}) more than the two apart"
                for (a, b), i in ((i['pair'], i) for i in found)) + '.')
        elif levers['interactions']:
            self.together.set_text(f"{len(levers['interactions'])} pair(s) measured together; no interaction is clear "
                                   'yet (their intervals still include zero).')
        else:
            self.together.set_text('')
        self.together.set_visible(bool(self.together.get_text()))
        self.fill_best(effects, quality)
        rows, note = model_rows(result)
        self.whatif.set_data(rows)
        self.whatif_note.set_text(note)
        while (row := self.experiment_list.get_first_child()) is not None:
            self.experiment_list.remove(row)
        active = self.manual_active()
        freezes = {e['experiment']: e for e in self.effects}
        if self.units:
            self.experiment_list.append(self.freeze_row(active))
            self.experiment_list.append(self.freeze_all_row(freezes.get('freeze-apps'), active))
        self.experiment_list.append(self.psr_row())

    def lever_row(self, spec, effect, progress, scale, included, quality):
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(xalign=0, label=spec['label'])
        title.add_css_class('heading')
        info.append(title)
        if spec['error']:
            detail = f"Unavailable: {spec['error']}"
        else:
            saving = spec['saving'] is not None and spec['current'] == spec['saving']
            other = (spec['normal'] if saving else spec['saving']) or spec['alternative']
            detail = f"Now {spec['current']}{' (power-saving)' if saving else ''} · or {other}"
        down, up = (quality[v]['tally'][spec['name']] for v in ('down', 'up'))
        detail += (f" · 👎 {down['on']['votes']}/{down['on']['windows']} blocks on" if down['on']['windows']
                   else f" · {spec['visible'] or 'not noticeable'}")
        if quality['up']['votes']:
            detail += f" · 👍 {up['on']['votes']}/{up['on']['windows']} on, {up['off']['votes']}/{up['off']['windows']} off"
        if report.qol_risk(quality, {spec['name']: True}) > report.QOL_RISK:
            detail += ' — kept off while you are here'
        info.append(self.muted(wrapped(label=detail, max_width_chars=56)))
        self.hint(info, spec['description'])
        row.append(info)
        bar = GainBar()
        bar.set_data(effect, scale)
        row.append(bar)
        if effect:
            evidence = f"{effect['blocks']['on']}+{effect['blocks']['off']} blocks, {effect['hours']:.1f} h"
            value = gain_text(effect)
        elif progress:
            evidence, value = f"{progress['on']}+{progress['off']} blocks so far", 'needs more'
        else:
            evidence, value = ('CPU setting: needs ~4× the time' if spec['name'] in report.CPU_LEVERS else ''), \
                'not measured'
        numbers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        numbers.append(Gtk.Label(label=value, xalign=1, width_chars=17))
        numbers.append(self.muted(Gtk.Label(label=evidence, xalign=1, width_chars=17)))
        row.append(numbers)
        check = Gtk.CheckButton(label='Include', valign=Gtk.Align.CENTER, active=included and not spec['error'])
        check.set_sensitive(not spec['error'] and spec['saving'] is not None)
        self.hint(check, 'Let automatic experiments vary this setting')
        check.connect('toggled', lambda b, n=spec['name']: self.set_pool(n, b.get_active()))
        row.append(check)
        return row

    def set_pool(self, name, included):
        pool = [n for n in (self.result.get('pool') or []) if n != name] + ([name] if included else [])
        pool = [c['name'] for c in self.settings() if c['name'] in pool]  # Catalog order.
        self.result['pool'] = pool
        if pool:
            self.command(['power-experiment', 'pool', *pool])
        else:
            self.command(['power-experiment', 'disable'], done='No settings included: automatic experiments are off.')

    def switch_experiments(self, _switch, on):
        if not self.syncing:
            self.command(['power-experiment', 'enable' if on else 'disable'])
        return False

    def set_goal(self, goal):
        self.result['goal'] = goal
        self.fill_best({e['experiment']: e for e in self.result['levers']['effects']}, self.quality)
        self.command(['power-experiment', 'goal', goal], done=None)

    def fill_best(self, effects, quality):
        """The best measured combination for the goal, among those unlikely to earn a 👎: the
        lowest draw (Battery), or the most likely to feel especially good, then the lowest draw (Comfort)."""
        levers, result = self.result['levers'], self.result
        goal = result.get('goal', 'battery')
        self.quality = quality
        self.syncing = True
        self.goal_buttons[goal].set_active(True)
        self.syncing = False
        fine = lambda c: report.qol_risk(quality, c) <= report.QOL_RISK
        key = ((lambda c: (-report.delight(quality, c), report.predicted(levers['coef'], c)))
               if goal == 'comfort' else None)
        best = report.best_config(levers, set(effects), result.get('energy'), result.get('power'), feasible=fine, key=key)
        self.best = None
        aim = 'feel especially good' if goal == 'comfort' else 'save battery'
        if not best:
            self.best_label.set_text('The best combination appears once settings have been measured.')
        elif not best['changes']:
            self.best_label.set_text(f'Your battery settings already {aim} best of what has been measured'
                                     + (' (no 👍 yet, so this is the same as Battery).'
                                        if goal == 'comfort' and not quality['up']['votes'] else '.'))
        else:
            self.best = best
            change = ', '.join(PHRASES.get(n, (n, n))[0 if best['config'][n] else 1] for n in best['changes'])
            if goal == 'comfort':
                now, then = (report.delight(quality, c) for c in (levers['current'], {**levers['current'], **best['config']}))
                self.best_label.set_text(f"Most likely to feel especially good: {change} → 👍 {100 * now:.0f}% → "
                                         f"{100 * then:.0f}% per block, {gain_text(best)} of battery per charge.")
            else:
                self.best_label.set_text(f"Best for battery, without likely 👎s: {change} → {gain_text(best)} per "
                                         'charge.')
        self.apply_button.set_visible(self.best is not None)
        self.apply_button.set_sensitive(self.experiment_state().get('status') not in ('running', 'paused'))

    def apply_best(self):
        if self.best:
            self.command(['power-experiment', 'apply'] + [f"{n}={'saving' if self.best['config'][n] else 'normal'}"
                                                          for n in self.best['changes']],
                         done='Applied until reboot (ASPM/APST until the next suspend).')

    def freeze_row(self, active):
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = Gtk.Label(xalign=0, label='Close an app (freeze it in B blocks)')
        title.add_css_class('heading')
        info.append(title)
        picker = Gtk.Box(spacing=8)
        self.unit_picker = Gtk.DropDown.new_from_strings(
            [f"{u['label']}  ({u['cpu_s'] // 60} CPU-min)" for u in self.units])
        picker.append(self.unit_picker)
        self.only_locked = Gtk.CheckButton(label='Only while the screen is locked', active=True)
        picker.append(self.only_locked)
        info.append(picker)
        note = wrapped(label='Measures everything the app costs, wakeups included. Frozen '
                         'apps stop responding until their B block ends.')
        note.add_css_class('muted')
        info.append(note)
        row.append(info)
        button = Gtk.Button(label='Start', valign=Gtk.Align.CENTER)
        button.set_sensitive(not active)
        button.connect('clicked', lambda _b: self.start('freeze', unit=self.units[self.unit_picker.get_selected()]['unit'],
                                                        only_locked=self.only_locked.get_active()))
        row.append(button)
        return row

    def freeze_all_row(self, effect, active):
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = Gtk.Label(xalign=0, label='Close every app (freeze all, while locked)')
        title.add_css_class('heading')
        info.append(title)
        info.append(self.muted(wrapped(label='Runs only while the screen is locked. Frozen blocks measure the '
                                       'platform floor; A − B is what the apps cost in the background.')))
        self.keep_t3 = Gtk.CheckButton(label='Keep T3 Code running (agent sessions would stall)', active=True)
        info.append(self.keep_t3)
        if effect and effect.get('b_watts') is not None:
            text_ = f"Floor with apps frozen: {effect['b_watts']:.1f} W · apps cost {effect['watts']:+.2f} W"
            if 'minutes' in effect:
                text_ += f" ({effect['watts_low']:+.2f} to {effect['watts_high']:+.2f}) · {effect['pairs']} pairs"
            result = Gtk.Label(xalign=0, label=text_)
            result.add_css_class('result')
            info.append(result)
        row.append(info)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        keep = lambda: ['--keep', 't3code' if self.keep_t3.get_active() else '//']  # '//' matches nothing.
        start = Gtk.Button(label='Start')
        start.set_sensitive(not active)
        start.connect('clicked', lambda _b: self.start('freeze-apps', extra=keep()))
        buttons.append(start)
        test = Gtk.Button(label='Test now (5 s)')
        self.hint(test, 'Freeze the same apps for 5 seconds right now, measure, and thaw')
        test.set_sensitive(not active)
        test.connect('clicked', lambda _b: (self.banner.set_text('Freezing apps for 5 seconds…'), self.command(
            ['power-experiment', 'test-freeze', '--seconds', '5'] + keep(), done=self.freeze_test_result)))
        buttons.append(test)
        row.append(buttons)
        return row

    def freeze_test_result(self, output):
        try:
            data = json.loads(output)
        except ValueError:
            return f'Freeze test: unreadable output {output[:200]!r}'
        units = data['units']
        frozen = [u for u in units if u['state'] == 'frozen']
        leaky = [f"{u['label']} {u['cpu_ms_while_frozen']} ms" for u in units if u['cpu_ms_while_frozen'] > 20]
        text = (f"Freeze test: {len(frozen)}/{len(units)} app scopes frozen for {data['seconds']:g} s"
                + (f"; still used CPU: {', '.join(leaky)}" if leaky else '; none used CPU while frozen') + '.')
        missed = [f"{', '.join(g['top'])} ({g['unit']}, {g['cpu_s']} CPU-s)" for g in data['uncovered'][:3]]
        if missed:
            text += ' Not freezable (outside app scopes): ' + '; '.join(missed) + '.'
        return text

    def psr_row(self):
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = Gtk.Label(xalign=0, label='Panel self-refresh (PSR) — boot-level')
        title.add_css_class('heading')
        info.append(title)
        psr = self.psr or {}
        boots, hours = psr.get('boots', {'on': 0, 'off': 0}), psr.get('hours', {'on': 0, 'off': 0})
        current = {'psr': 'on', 'default': 'off'}.get(self.psr_arm, 'unknown')
        info.append(self.muted(wrapped(label=(
            f"This boot: PSR {current}. Logged on battery: PSR on {boots['on']} boots / {hours['on']:.1f} h, "
            f"off {boots['off']} boots / {hours['off']:.1f} h. Needs 2+ boots each. PSR was disabled by "
            "nixos-hardware for hangs reported in 2024; if the desktop stalls, reboot to the default entry."))))
        if 'watts' in psr:
            result = Gtk.Label(xalign=0, label=(
                f"Result: PSR on saves {psr['watts']:+.2f} W ({psr['watts_low']:+.2f} to {psr['watts_high']:+.2f})"
                + (f" → {psr['minutes']:+.0f} min per charge" if 'minutes' in psr else '')))
            result.add_css_class('result')
            info.append(result)
        row.append(info)
        # Suggest the arm with less battery time so far.
        suggested = 'psr' if hours['on'] <= hours['off'] else 'default'
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        for arm, label in (('psr', 'Next boot: PSR on'), ('default', 'Next boot: default')):
            button = Gtk.Button(label=label)
            if arm == suggested:
                button.add_css_class('suggested-action')
            button.connect('clicked', lambda _b, a=arm: self.command(
                ['power-experiment', 'next-boot', a],
                done=f"Next boot set to {'PSR on' if a == 'psr' else 'the default entry'} — restart when convenient."))
            buttons.append(button)
        row.append(buttons)
        return row

    @staticmethod
    def muted(label):
        label.add_css_class('muted')
        return label

    def start(self, names, unit=None, only_locked=False, extra=()):
        minutes = 60 * BATTERY_HOURS[self.minutes.get_selected()]
        names = [names] if isinstance(names, str) else list(names)
        command = ['power-experiment', 'start', *names, '--minutes', str(minutes)]
        if unit:
            command += ['--unit', unit]
        if only_locked:
            command.append('--only-locked')
        self.command(command + list(extra))

    def command(self, argv, done=None):
        """Run a CLI; `done` is a note to pin, or a function of stdout returning one."""
        process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)

        def finished(proc, res):
            _, out, err = proc.communicate_utf8_finish(res)
            if not proc.get_successful():
                self.banner.set_text(f'Command failed: {(err or "").strip()[:300]}')
                return
            message = done(out or '') if callable(done) else done
            if message:
                self.note = message  # Kept under the status line until the panel restarts.
                self.refresh_status()
            # Settings changed: re-read them (and the rows) shortly.
            GLib.timeout_add(800, lambda: (self.refresh_status(), self.reload()) and False)
        process.communicate_utf8_async(None, None, finished)

    @staticmethod
    def estimate():
        """battery-eta's latest estimate, if fresh."""
        try:
            data = json.loads((RUNTIME / 'eta.json').read_text())
            return data if time.time() - data.get('updated', 0) < 15 else None
        except (OSError, ValueError):
            return None

    def refresh_status(self):
        battery = next((p for p in sorted(Path('/sys/class/power_supply').glob('BAT*'))), None)
        try:
            pct = int((battery / 'capacity').read_text())
            status = (battery / 'status').read_text().strip()
            watts = int((battery / 'current_now').read_text()) * int((battery / 'voltage_now').read_text()) / 1e12
            charge = int((battery / 'charge_now').read_text()) * int((battery / 'voltage_now').read_text()) / 1e12
            line = f'{pct}% · {status.lower()}'
            estimate = self.estimate()
            if estimate and estimate.get('hours') is not None:
                line = f"{pct}% charge, {estimate['energy_pct']}% energy · {status.lower()} · {abs(watts):.1f} W"
                interval = f"{duration(estimate['low'])}–{duration(estimate['high'])}"
                if status == 'Discharging':
                    line += f" · {duration(estimate['hours'])} left (80%: {interval})"
                elif status == 'Charging':
                    if estimate.get('ratio') is not None:
                        line += (f" · ×{estimate['ratio']:.1f} minutes of use per minute charging "
                                 f"(80%: ×{estimate['ratio_low']:.1f}–{estimate['ratio_high']:.1f})")
                    line += f" · {duration(estimate['hours'])} to {estimate['target']:.0f}% (80%: {interval})"
            elif status == 'Discharging' and watts > 0.5:
                line += f' · {watts:.1f} W · ~{duration(charge / watts)} left at this draw'
            self.status.set_text(line)
        except (OSError, ValueError, TypeError, AttributeError):
            self.status.set_text('')
        state = self.experiment_state()
        status, auto = state.get('status'), state.get('mode') == 'auto'
        enabled = experiment.ENABLED.exists()
        self.syncing = True  # Reflect the files without the widgets acting on it.
        self.auto_switch.set_active(enabled)
        self.syncing = False
        self.stop_button.set_visible(not auto and status in ('running', 'paused'))
        recent, rest = qol.votes(), qol.resting()
        last = (f"Last vote: {'👍' if recent[-1][1] == 'up' else '👎'} at "
                f"{time.strftime('%H:%M', time.localtime(recent[-1][0]))}. " if recent else '')
        self.vote_note.set_text(last + (f'Experiments rest {rest / 60:.0f} more min.' if rest else
                                        '👍 for anything unexpectedly nice, 👎 when something is off (it restores '
                                        'your settings at once); also next to the battery.'))
        self.resume_button.set_visible(rest > 0)
        frozen = ''
        if state.get('units') and status in ('running', 'paused'):
            states = [experiment.freeze_get(u) for u in experiment.live_units(state['units'])]
            frozen = f" Apps frozen right now: {states.count('frozen')}/{len(states)}."
        settling = 'Settling (first minute ignored).' if time.time() < state.get('washout_until', 0) else ''
        if auto and status == 'running':
            config = ', '.join(PHRASES.get(n, (n, n))[0 if arm == 'B' else 1] for n, arm in state['config'].items())
            plan = state.get('plan') or {}
            allowed = (f" ({plan['allowed']} of {plan['combinations']} combinations allowed)"
                       if plan.get('combinations') else '')
            self.banner.set_text(f"Block {state['block']}: {config}{allowed}. {settling}")
        elif auto and status == 'paused':
            self.banner.set_text(f"Paused: {state.get('reason')}; your settings are restored meanwhile.")
        elif status == 'running':
            names = state.get('names') or [state.get('name')]
            arms = ', '.join(f"{n} {state['config'][n]}" for n in names) if state.get('config') else state['arm']
            progress = (f"{duration(state.get('collected', 0) / 3600)} of {duration(state['target_seconds'] / 3600)} "
                        'on battery collected. ' if state.get('target_seconds') else '')
            self.banner.set_text(f"Running: {state['label']} — block {state['block']}: {arms} (A = as before). "
                                 + progress + settling + frozen)
        elif status == 'paused':
            reason = state.get('reason')
            if reason == 'waiting for the screen to lock':
                reason += ' (apps keep running until you lock with Ctrl+Alt+L)'
            self.banner.set_text(f"Paused: {state['label']} — {reason}. Settings restored meanwhile." + frozen)
        elif enabled:
            self.banner.set_text('On; starting…')
        else:
            self.banner.set_text('Off. Turn on to let the laptop test settings by itself while on battery.')
        if self.note:
            self.banner.set_text(self.banner.get_text() + '\n' + self.note)


CSS = b"""
window { background: transparent; }
#battery-card { background: @menu_bg; color: @menu_fg; border: 1px solid @menu_border;
                border-left-color: @menu_border_strong; border-radius: 12px;
                padding: 12px 14px; font-family: sans-serif; font-size: 13px; }
#battery-card .muted { color: @menu_muted; font-size: 12px; }
#battery-card .banner { background: @menu_card; border-radius: 10px; padding: 8px 10px; }
#battery-card .experiment { padding: 8px 10px; }
#battery-card .result { font-weight: bold; font-size: 12px; }
#battery-card list { background: @menu_card; border-radius: 10px; }
#battery-card button:hover { background: @menu_hover; }
#battery-card .thumb { font-family: "Font Awesome 7 Free"; font-weight: 900; }
popover.hint > contents { background: @menu_card; color: @menu_fg; padding: 6px 9px; font-size: 12px; }
#battery-card button.vote-up .thumb { color: #8fdf8f; }
#battery-card button.vote-down .thumb { color: #e5484d; }
"""


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == '--render':
        render(sys.argv[2], *(sys.argv[3:4] or ['Day']))
    else:
        raise SystemExit(BatteryPanel().run([]))
