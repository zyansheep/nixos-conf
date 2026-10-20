#!/usr/bin/env python3
"""Layer-shell battery panel: history timeline, sleep drain, what-if runtime and
power experiments, drawn from the waybar-monitor power log (see report.py)."""
import json
import math
import os
import threading
import time
import traceback
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

import eta  # noqa: E402,F401 - first: it limits BLAS threads before numpy loads
import experiment  # noqa: E402
import qol  # noqa: E402
import report  # noqa: E402
from panel_data import (GONE, NOTE_SECONDS, History, apply_note, banner_text, command_note, duration,  # noqa: E402
                        gather, health_text, interaction_clear, lever_rows, no_levers, picker, read_battery,
                        read_estimate, status_line, typical_text, value_text, what_if_note)
from report import PHRASES  # noqa: E402

# Categorical slots in validated order (dark surface #222226): adjacent stack
# pairs clear CVD ΔE 8.4 / normal 19.3. Neutrals carry the always-on baseline.
COLORS = {
    'Idle chip': '#707076', 'Rest of system': '#aaaab0',
    'Browser': '#3987e5', 'Builds & EDA': '#d95926', 'Coding tools': '#199e70', 'Display': '#c98500',
    'Chat & media': '#d55181', 'Other apps': '#008300', 'Desktop & system': '#9085e9',
}
STACK = list(COLORS)
SURFACE, CARD = (0x22 / 255, 0x22 / 255, 0x26 / 255), (0x34 / 255, 0x34 / 255, 0x37 / 255)
INK, MUTED, GRID = (1, 1, 1), (1, 1, 1, .68), (1, 1, 1, .08)
RANGES = {'6 h': 6 * 3600, 'Day': 24 * 3600, 'Week': 7 * 24 * 3600}
BATTERY_HOURS = [1, 2, 4, 8, 24]  # Experiment length choices, in hours of battery time.
DEBOUNCE = 0.15  # s: reloads this close together (holding ← or →) load only the last one.
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
            groups, idle = {}, {}
            for p in points:
                for name, watts in p['groups'].items():
                    groups[name] = groups.get(name, 0) + watts / len(points)
                for name, watts in (p.get('idle') or {}).items():
                    idle[name] = idle.get(name, 0) + watts / len(points)
            merged.append({'groups': groups, 'idle': idle, 'points': points, 'battery': sum(p['st'] == 'D' for p in points) * 2 > len(points),
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
                    if name == report.IDLE and set(column['idle']) != {report.IDLE}:  # Its rails, once learned.
                        lines += [(f'{w:.1f} W', f'   {part}', None) for part, w in
                                  sorted(column['idle'].items(), key=lambda kv: -kv[1]) if w >= .05]
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
    rows = result.get('estimates') or []
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
        # Not a bound method: one would hold the bar from its own draw func, a
        # cycle PyGObject can't collect (every rebuilt row leaked).
        self.set_draw_func(GainBar.paint)

    def set_data(self, gain, scale):
        if (gain, scale) != (self.gain, self.scale):
            self.gain, self.scale = gain, scale
            self.queue_draw()

    @staticmethod
    def paint(bar, cr, width, height):
        low, high = bar.scale
        sx = lambda m: 1 + (min(max(m, low), high) - low) / (high - low) * (width - 2)
        cr.set_source_rgba(1, 1, 1, .35)
        cr.set_line_width(1)
        cr.move_to(round(sx(0)) + .5, 1)
        cr.line_to(round(sx(0)) + .5, height - 1)
        cr.stroke()
        if not bar.gain or bar.gain.get('minutes') is None or not math.isfinite(bar.gain['minutes']):
            return
        color = COLORS['Coding tools'] if bar.gain['minutes'] >= 0 else COLORS['Builds & EDA']
        x0, x1 = sorted((sx(0), sx(bar.gain['minutes'])))
        rounded(cr, x0, 4, max(2, x1 - x0), height - 8, 3)
        cr.set_source_rgba(*rgb(color))
        cr.fill()
        if bar.gain.get('low') is not None and math.isfinite(bar.gain['low']) and math.isfinite(bar.gain['high']):
            cr.set_source_rgba(1, 1, 1, .85)
            cr.set_line_width(1.5)
            cy = height / 2
            cr.move_to(sx(bar.gain['low']), cy)
            cr.line_to(sx(bar.gain['high']), cy)
            for edge in (bar.gain['low'], bar.gain['high']):
                cr.move_to(sx(edge), cy - 4)
                cr.line_to(sx(edge), cy + 4)
            cr.stroke()


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
        self.generation, self.busy, self.shown = 0, False, (0, 1)
        self.loaded = None
        self.history = History()
        # One loader thread takes the latest reload request (see `work`).
        self.wake, self.request, self.requested, self.worker = threading.Condition(), None, 0.0, None
        self.poll = 0
        self.catalog, self.units, self.effects = [], [], []
        self.psr, self.psr_arm = None, None
        self.result, self.best = None, None
        self.lever_names, self.lever_widgets, self.hints = None, {}, {}
        self.unit_names, self.freeze_allowed = [], False
        self.syncing = False  # Set while widgets are updated from state, so they don't echo it back.
        self.note, self.note_until = '', None  # The last command's outcome, under the banner (see set_note).
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
        title = Gtk.Label(label='Battery', xalign=0, valign=Gtk.Align.START)
        title.add_css_class('title-2')
        header.append(title)
        # Time left awake on battery, and asleep from now on a second line.
        self.status = self.muted(wrapped(hexpand=True, valign=Gtk.Align.CENTER))
        header.append(self.status)
        close = Gtk.Button.new_from_icon_name('window-close-symbolic')
        close.add_css_class('flat')
        close.set_valign(Gtk.Align.START)
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
        # A part of the data that failed: the rest still shows (panel_data.gather).
        self.problems = wrapped(visible=False)
        self.problems.add_css_class('problem')
        card.append(self.problems)

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
        experiments. Built once; fill_experiments updates it in place."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Gtk.Box(spacing=10)
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.auto_switch.connect('state-set', self.switch_experiments)
        self.hint(self.auto_switch, 'Off (power-experiment disable) puts every setting back to stock, '
                                    'applied ones too')
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
        # What the numbers below were computed on, and how fresh the models are.
        self.health = self.muted(wrapped())
        self.health.add_css_class('health')
        page.append(self.health)

        page.append(self.section('Settings'))
        self.lever_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lever_list.add_css_class('boxed-list')
        page.append(self.lever_list)
        self.accuracy = self.muted(wrapped())
        page.append(self.accuracy)
        # Whether more testing is worth it: what it would save and cost.
        self.worth = self.muted(wrapped())
        page.append(self.worth)
        self.worth_grid, self.worth_rows = Gtk.Grid(column_spacing=18, row_spacing=2, margin_start=12), None
        page.append(self.worth_grid)
        self.together = self.muted(wrapped())
        page.append(self.together)
        best = Gtk.Box(spacing=8)
        goals = Gtk.Box(valign=Gtk.Align.CENTER)
        goals.add_css_class('linked')
        self.goal_buttons, group = {}, None
        for name, label in (('battery', 'Battery'), ('comfort', 'Comfort')):
            button = Gtk.ToggleButton(label=label, sensitive=False)  # Until the first load.
            button.set_group(group)
            group = group or button
            button.connect('toggled', lambda b, n=name: b.get_active() and not self.syncing and self.set_goal(n))
            goals.append(button)
            self.goal_buttons[name] = button
        best.append(goals)
        self.best_label = wrapped(hexpand=True)
        self.best_label.add_css_class('result')
        best.append(self.best_label)
        self.apply_button = Gtk.Button(label='Apply', valign=Gtk.Align.CENTER, visible=False)
        self.hint(self.apply_button, 'Until reboot, on battery: on AC they go back to stock and return on '
                                     'battery. Risky settings (ASPM, APST, GPU clocks) and quarantined ones are '
                                     'left out; the note under the banner says which.')
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
        for row in (self.freeze_row(), self.freeze_all_row(), self.psr_row()):
            self.experiment_list.append(row)
        # The two freezing rows show only with app scopes to freeze.
        self.freeze_rows = [self.experiment_list.get_row_at_index(i) for i in (0, 1)]
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
        self.poll = self.poll or GLib.timeout_add_seconds(2, self.tick)
        try:
            self.refresh_status()
            self.reload()
        except Exception:  # noqa: BLE001 - the panel stays usable; the tick retries
            traceback.print_exc()

    def show_page(self, _action, page):
        name = {'sleep': 'timeline', 'whatif': 'experiments', 'savings': 'experiments'}.get(page.get_string(),
                                                                                           page.get_string())
        if self.stack.get_child_by_name(name):
            self.stack.set_visible_child_name(name)
        if not self.window.get_visible():
            self.toggle()

    def hint(self, widget, text):
        """Show `text` (or what `text()` returns) at once on hover. The handlers
        reach the widget through their controller: a closure holding the widget
        is a cycle PyGObject can't collect, and leaked every rebuilt row."""
        motion = Gtk.EventControllerMotion()
        motion.connect('enter', lambda ctl, *_: self.show_hint(ctl.get_widget(), text() if callable(text) else text))
        motion.connect('leave', lambda *_: self.hint_popover.popdown())
        widget.add_controller(motion)

    def show_hint(self, widget, text):
        found, bounds = widget.compute_bounds(self.hint_anchor)
        if not found or not text:
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
        with self.wake:
            self.request = None
        self.generation += 1  # Cancels a load in flight.
        self.busy = False
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
        """Every 2 s while open: the status line; each minute (at Now) the period."""
        if not self.window.get_visible():
            self.poll = 0
            return False
        try:
            self.refresh_status()
            if not self.busy and self.loaded and time.time() - self.loaded > 60 and self.offset == 0:
                self.reload()
        except Exception:  # noqa: BLE001 - raising here would stop the timer for good
            traceback.print_exc()
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
        offset = 0 if delta is None else min(0, self.offset + delta)
        if offset != self.offset:  # → at Now, or Now there, changes nothing.
            self.offset = offset
            self.reload()

    def reload(self):
        self.generation += 1
        start, end = self.shown = self.span()
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
        self.busy = True
        with self.wake:
            now = time.monotonic()
            # A reload right after another (a held arrow key) waits for the last of them.
            self.request = (self.generation, start, end, now + DEBOUNCE if now - self.requested < DEBOUNCE else now)
            self.requested = now
            self.wake.notify()
        if self.worker is None:
            self.worker = threading.Thread(target=self.work, name='battery-panel-load', daemon=True)
            self.worker.start()

    def work(self):
        """The one loader thread: the latest request, once its debounce is over.
        A newer request cancels the running load between two of its parts."""
        while True:
            with self.wake:
                while self.request is None or self.request[3] > time.monotonic():
                    self.wake.wait(None if self.request is None else self.request[3] - time.monotonic())
                generation, start, end, _ = self.request
                self.request = None
            self.load(generation, start, end, self.history)

    def load(self, generation, start, end, history):
        """The period's charts as soon as they are ready, then the rest."""
        try:
            result = gather(start, end, history, cancelled=lambda: generation != self.generation,
                            on_period=lambda partial: GLib.idle_add(self.apply, generation, partial))
        except BaseException as error:  # noqa: BLE001 - last resort; a SystemExit must not end the one loader
            traceback.print_exc()
            result = {'error': f'{type(error).__name__}: {error}'}
        if result is not None:  # None: a newer reload took over.
            GLib.idle_add(self.apply, generation, result)

    def apply(self, generation, result):
        """A load's result, or (`pending`) its period part while the rest is computed."""
        if generation != self.generation:
            return False
        try:
            self.show(result)
        except Exception as error:  # noqa: BLE001 - an idle callback; say so instead of a stuck spinner
            traceback.print_exc()
            self.spinner.stop()
            self.busy = False
            self.show_problems({'the panel': f'{type(error).__name__}: {error}'})
        return False

    def show(self, result):
        pending = result.get('pending', False)
        if not pending:
            self.spinner.stop()
            self.loaded, self.busy = time.time(), False
        self.show_problems(result.get('errors') or {})
        if 'error' in result:
            # Nothing of the previous period stays under the new one's label.
            self.summary.set_text(result['error'])
            self.timeline.set_data([], self.shown, [])
            self.sleep.set_data([], self.shown, None)
            self.whatif.set_data([])
            self.whatif_note.set_text('')
            for chart in (self.timeline, self.sleep, self.whatif):
                chart.loading = False
            self.fill_legend([])
            return
        self.timeline.loading = self.sleep.loading = False
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
        if pending:
            return  # The what-if stays faded: its estimates come with the rest.
        self.whatif.loading = False
        self.effects = result['effects']
        self.catalog, self.units = result['catalog'], result['units']
        self.psr, self.psr_arm = result.get('psr'), result.get('psr_arm')
        self.result = result
        self.fill_experiments()
        self.refresh_status()  # The banner shows the quarantine, now known.

    def show_problems(self, errors):
        if errors:
            parts = '; '.join(f'{part} ({error})' for part, error in errors.items())
            self.problems.set_text(f"Couldn't compute {parts}. Everything else is shown; details: "
                                   'journalctl --user -u battery-panel, or battery-panel-check.')
        self.problems.set_visible(bool(errors))

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
            state = json.loads((RUNTIME / 'experiment.json').read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def settings(self):
        return [c for c in self.catalog if c['name'] in report.SETTING]

    def manual_active(self):
        state = self.experiment_state()
        return state.get('mode') != 'auto' and state.get('status') in ('running', 'paused')

    def fill_experiments(self):
        """The Experiments tab from the latest result, in place: the minute's
        reload must not reset a choice being made (the app picker, the boxes)."""
        result = self.result
        levers, plan = result.get('levers') or no_levers(), result.get('plan')
        names = {c['name']: c['label'] for c in self.catalog}
        risky = [names.get(n, n) for n in result.get('risky') or () if n in names]
        self.experiments_note.set_text(
            typical_text(result) + 'While on, every 4-minute block on battery sets the included settings to the '
            'combination the battery model expects to learn most from — among those unlikely to bother you, or any '
            'while the screen is locked — and learns from your 👎 which settings to avoid and from your 👍 which are '
            'worth keeping (the Comfort goal). Gains are minutes per full charge (90% intervals), each for that '
            'setting alone with the others at your settings.'
            + (f" {', '.join(risky)}: risky for the hardware, never in automatic runs." if risky else ''))
        self.health.set_text(health_text(result.get('health')))
        self.fill_levers(lever_rows(self.settings(), levers, result.get('quality'), set(result.get('pool') or []),
                                    result.get('risky') or (), result.get('quarantine') or {}))
        if plan and plan.get('accuracy') is not None:
            least = ', '.join(PHRASES.get(n, (n, n))[0] for n, on in (plan.get('least_certain') or {}).items()
                              if on) or 'all off'
            self.accuracy.set_text(f"Battery model: for the {plan['allowed']} of {plan['combinations']} combinations "
                                   f"allowed now, predictions are within ±{plan['accuracy']:.1f} W on average (90%); "
                                   f"least certain: {least}. That is where the next blocks look.")
        else:
            self.accuracy.set_text('Include settings to experiment with.')
        text, rows = value_text((plan or {}).get('value'), result.get('typical'), result.get('retest'))
        self.worth.set_text(text)
        self.worth.set_visible(bool(text))
        if rows != self.worth_rows:  # Rebuilt only when they change.
            while (child := self.worth_grid.get_first_child()) is not None:
                self.worth_grid.remove(child)
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    self.worth_grid.attach(self.muted(Gtk.Label(label=cell, xalign=0)), j, i, 1, 1)
            self.worth_rows = rows
        self.worth_grid.set_visible(bool(rows))
        interactions = levers.get('interactions') or []
        found = [i for i in interactions if interaction_clear(i)]
        if found:
            self.together.set_text('Together: ' + '; '.join(
                f"{PHRASES.get(a, (a,))[0]} with {PHRASES.get(b, (b,))[0]} saves {i['watts']:+.2f} W "
                f"({i['watts_low']:+.2f} … {i['watts_high']:+.2f}) more than the two apart"
                + (f" ({'p < 0.001' if i['p'] < 0.001 else f'p = {i['p']:.2g}'})" if isinstance(i.get('p'), float)
                   else '')
                for (a, b), i in ((i['pair'], i) for i in found)) + '.')
        elif interactions:
            self.together.set_text(f'{len(interactions)} pair(s) measured together; no interaction is clear yet '
                                   '(each tested against how many would look clear by chance).')
        else:
            self.together.set_text('')
        self.together.set_visible(bool(self.together.get_text()))
        self.fill_best()
        self.whatif.set_data(result.get('estimates') or [])
        self.whatif_note.set_text(what_if_note(result))
        active = self.manual_active()
        self.fill_units()
        for row in self.freeze_rows:
            row.set_visible(bool(self.units))
        self.freeze_allowed = not active and bool(self.units)
        self.freeze_sensitivity()
        freezes = {e['experiment']: e for e in self.effects}
        self.fill_freeze_all(freezes.get('freeze-apps'), active)
        self.fill_psr()

    def fill_levers(self, rows):
        """The settings rows, updated in place; rebuilt only when the settings
        themselves change (a rebuild closes a hint and drops focus)."""
        names = [row['name'] for row in rows]
        if names != self.lever_names:
            self.hint_popover.popdown()  # Its widget may be about to go.
            while (child := self.lever_list.get_first_child()) is not None:
                self.lever_list.remove(child)
            self.lever_widgets = {}
            for row in rows:
                box, self.lever_widgets[row['name']] = self.lever_row(row['name'], row['label'])
                self.lever_list.append(box)
            self.lever_names = names
        scale = shared_scale([row['effect'] for row in rows])
        self.syncing = True
        try:
            for row in rows:
                widgets = self.lever_widgets[row['name']]
                widgets['detail'].set_text(row['detail'])
                widgets['bar'].set_data(row['effect'], scale)
                widgets['value'].set_text(row['value'])
                widgets['evidence'].set_text(row['evidence'])
                widgets['check'].set_active(row['included'])
                widgets['check'].set_sensitive(row['sensitive'])
                self.hints[row['name']] = (row['description'], row['include_hint'])
        finally:
            self.syncing = False

    def lever_row(self, name, label):
        """One setting's row, and its widgets for fill_levers."""
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(xalign=0, label=label)
        title.add_css_class('heading')
        info.append(title)
        detail = self.muted(wrapped(max_width_chars=56))
        info.append(detail)
        self.hint(info, lambda: self.hints.get(name, ('', ''))[0])
        row.append(info)
        bar = GainBar()
        row.append(bar)
        numbers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        value = Gtk.Label(xalign=1, width_chars=17)
        numbers.append(value)
        evidence = self.muted(Gtk.Label(xalign=1, width_chars=17))
        numbers.append(evidence)
        row.append(numbers)
        check = Gtk.CheckButton(label='Include', valign=Gtk.Align.CENTER)
        self.hint(check, lambda: self.hints.get(name, ('', ''))[1])
        check.connect('toggled', lambda b: self.syncing or self.set_pool(name, b.get_active()))
        row.append(check)
        return row, {'detail': detail, 'bar': bar, 'value': value, 'evidence': evidence, 'check': check}

    def set_pool(self, name, included):
        """One box, one name: `pool add|remove NAME` leaves the rest of the chosen
        pool alone (quarantined settings stay chosen for `release`, risky ones
        added with --risky stay in). The panel never passes --risky: a risky
        setting goes in only from the CLI, and its box comes out greyed.
        Emptying the pool doesn't run `disable` (which puts every setting back
        to stock, applied ones too): the runner stays on, with nothing to test."""
        if self.result is None:
            return
        pool = [n for n in (self.result.get('pool') or []) if n != name] + ([name] if included else [])
        self.result['pool'] = [c['name'] for c in self.settings() if c['name'] in pool]  # Catalog order.
        empty = None if self.result['pool'] else ('No settings included: automatic experiments have nothing to '
                                                  'test until you include one (the switch turns them off).')
        self.command(['power-experiment', 'pool', 'add' if included else 'remove', name], done=empty)

    def switch_experiments(self, _switch, on):
        if not self.syncing:
            self.command(['power-experiment', 'enable' if on else 'disable'])
        return False

    def set_goal(self, goal):
        if self.result is None:
            return
        self.result['goal'] = goal
        self.fill_best()
        # goal --apply applies nothing while a run owns the settings (as Apply is insensitive then);
        # paused because testing isn't worth it, the runner sets the goal's combination itself.
        state = self.experiment_state()
        owned = state.get('status') in ('running', 'paused')
        assignments = self.goal_assignments(goal)
        self.command(['power-experiment', 'goal', goal, '--apply'],
                     done=f'Goal: {goal.capitalize()}. Paused experiments switch to its combination within a minute.'
                     if owned and state.get('settled') else
                     f'Goal: {goal.capitalize()}. Nothing applied: experiments own the settings.' if owned else
                     lambda out, err: apply_note(assignments, out, err))

    def goal_assignments(self, goal):
        """What `goal --apply` sets: recommend.json's combination, the one shown
        while it is fresh; else the CLI computes its own (None: not known here)."""
        if (self.result or {}).get('recs_source') != 'battery-eta':
            return None
        return ((self.result.get('recs') or {}).get(goal) or {}).get('apply') or []

    def fill_best(self):
        """Each goal's best measured combination, the chosen goal's first, from
        ONE source (panel_data.recommendations): what the battery menu shows,
        what switching goals applies (unless automatic experiments own the
        settings) and what Apply applies."""
        result = self.result
        goal = result.get('goal') if result.get('goal') in report.GOALS else 'battery'
        self.syncing = True
        try:
            for name, button in self.goal_buttons.items():
                button.set_sensitive(True)
                if name == goal:
                    button.set_active(True)
        finally:
            self.syncing = False
        recs = result.get('recs') or {}
        self.best = recs.get(goal) if (recs.get(goal) or {}).get('apply') else None
        order = (goal,) + tuple(g for g in report.GOALS if g != goal)
        lines = [f"{g.capitalize()}: {(recs.get(g) or {}).get('text') or 'nothing measured yet'}" for g in order]
        if result.get('recs_source') == 'panel':
            lines.append("(Computed here: battery-eta's recommend.json is missing or over an hour old.)")
        self.best_label.set_text('\n'.join(lines))
        self.apply_button.set_visible(self.best is not None)
        self.apply_button.set_sensitive(self.experiment_state().get('status') not in ('running', 'paused'))

    def apply_best(self):
        if self.best:
            assignments = list(self.best['apply'])
            self.command(['power-experiment', 'apply', *assignments],
                         done=lambda out, err: apply_note(assignments, out, err))

    def freeze_row(self):
        row = Gtk.Box(spacing=10)
        row.add_css_class('experiment')
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = Gtk.Label(xalign=0, label='Close an app (freeze it in B blocks)')
        title.add_css_class('heading')
        info.append(title)
        choice = Gtk.Box(spacing=8)
        self.unit_model = Gtk.StringList()
        self.unit_picker = Gtk.DropDown(model=self.unit_model)
        self.unit_picker.connect('notify::selected', lambda *_: self.freeze_sensitivity())
        choice.append(self.unit_picker)
        self.only_locked = Gtk.CheckButton(label='Only while the screen is locked', active=True)
        choice.append(self.only_locked)
        info.append(choice)
        note = wrapped(label='Measures everything the app costs, wakeups included. Frozen '
                         'apps stop responding until their B block ends.')
        note.add_css_class('muted')
        info.append(note)
        row.append(info)
        self.freeze_start = Gtk.Button(label='Start', valign=Gtk.Align.CENTER)
        self.freeze_start.connect('clicked', lambda _b: self.start_freeze())
        row.append(self.freeze_start)
        return row

    def fill_units(self):
        """The app picker (panel_data.picker), replaced only when the set of apps
        changes, keeping the chosen app by unit name (the order follows CPU
        time); if it exited, a placeholder is chosen and Start waits for a choice."""
        chosen = self.chosen_unit()
        if chosen is None and self.unit_names[:1] == [None]:
            chosen = GONE  # The placeholder is still chosen.
        update = picker(self.units, self.unit_names, chosen)
        if update is None:
            return
        self.unit_names, labels, index = update
        self.unit_model.splice(0, self.unit_model.get_n_items(), labels)
        self.unit_picker.set_selected(index)
        self.freeze_sensitivity()

    def chosen_unit(self):
        index = self.unit_picker.get_selected()
        return self.unit_names[index] if 0 <= index < len(self.unit_names) else None

    def freeze_sensitivity(self):
        self.freeze_start.set_sensitive(self.freeze_allowed and self.chosen_unit() is not None)

    def start_freeze(self):
        unit = self.chosen_unit()
        if unit is not None:
            self.start('freeze', unit=unit, only_locked=self.only_locked.get_active())

    def freeze_all_row(self):
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
        self.freeze_all_result = Gtk.Label(xalign=0, visible=False)
        self.freeze_all_result.add_css_class('result')
        info.append(self.freeze_all_result)
        row.append(info)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        keep = lambda: ['--keep', 't3code' if self.keep_t3.get_active() else '//']  # '//' matches nothing.
        self.freeze_all_start = Gtk.Button(label='Start')
        self.freeze_all_start.connect('clicked', lambda _b: self.start('freeze-apps', extra=keep()))
        buttons.append(self.freeze_all_start)
        self.freeze_test = Gtk.Button(label='Test now (5 s)')
        self.hint(self.freeze_test, 'Freeze the same apps for 5 seconds right now, measure, and thaw')
        self.freeze_test.connect('clicked', lambda _b: self.command(
            ['power-experiment', 'test-freeze', '--seconds', '5'] + keep(),
            done=lambda out, _err: (self.freeze_test_result(out), None), running='Freezing apps for 5 seconds…'))
        buttons.append(self.freeze_test)
        row.append(buttons)
        return row

    def fill_freeze_all(self, effect, active):
        text_ = ''
        if effect and effect.get('b_watts') is not None:
            text_ = f"Floor with apps frozen: {effect['b_watts']:.1f} W · apps cost {effect['watts']:+.2f} W"
            if 'minutes' in effect:
                text_ += f" ({effect['watts_low']:+.2f} to {effect['watts_high']:+.2f}) · {effect['pairs']} pairs"
        self.freeze_all_result.set_text(text_)
        self.freeze_all_result.set_visible(bool(text_))
        self.freeze_all_start.set_sensitive(not active)
        self.freeze_test.set_sensitive(not active)

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
        self.psr_note = self.muted(wrapped())
        info.append(self.psr_note)
        self.psr_result = Gtk.Label(xalign=0, visible=False)
        self.psr_result.add_css_class('result')
        info.append(self.psr_result)
        row.append(info)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.psr_buttons = {}
        for arm, label in (('psr', 'Next boot: PSR on'), ('default', 'Next boot: default')):
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _b, a=arm: self.command(
                ['power-experiment', 'next-boot', a],
                done=f"Next boot set to {'PSR on' if a == 'psr' else 'the default entry'} — restart when convenient."))
            buttons.append(button)
            self.psr_buttons[arm] = button
        row.append(buttons)
        return row

    def fill_psr(self):
        psr = self.psr or {}
        boots, hours = psr.get('boots', {'on': 0, 'off': 0}), psr.get('hours', {'on': 0, 'off': 0})
        current = {'psr': 'on', 'default': 'off'}.get(self.psr_arm, 'unknown')
        self.psr_note.set_text(
            f"This boot: PSR {current}. Logged on battery: PSR on {boots['on']} boots / {hours['on']:.1f} h, "
            f"off {boots['off']} boots / {hours['off']:.1f} h. Needs {report.MIN_BOOTS}+ boots each (with "
            f"{report.BOOT_MINUTES}+ minutes on battery). PSR was disabled by "
            "nixos-hardware for hangs reported in 2024; if the desktop stalls, reboot to the default entry.")
        result = ''
        if 'watts' in psr:
            result = (f"Result: PSR on saves {psr['watts']:+.2f} W ({psr['watts_low']:+.2f} to {psr['watts_high']:+.2f})"
                      + (f" → {psr['minutes']:+.0f} min per charge" if 'minutes' in psr else ''))
        self.psr_result.set_text(result)
        self.psr_result.set_visible(bool(result))
        # Suggest the arm with less battery time so far.
        suggested = 'psr' if hours['on'] <= hours['off'] else 'default'
        for arm, button in self.psr_buttons.items():
            (button.add_css_class if arm == suggested else button.remove_css_class)('suggested-action')

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

    def command(self, argv, done=None, running=''):
        """Run a CLI. Its outcome goes under the banner (panel_data.command_note):
        `done` is a note, or a function of (stdout, stderr) returning (note,
        seconds); a failure, or a refusal on stderr, stays NOTE_SECONDS.
        `running` shows meanwhile."""
        self.set_note(running)  # The last command's outcome no longer applies.
        try:
            process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as error:  # Not on PATH, say.
            self.set_note(*command_note(False, '', error.message))
            return

        def finished(proc, res):
            try:
                _, out, err = proc.communicate_utf8_finish(res)
                ok = proc.get_successful()
            except GLib.Error as error:
                out, err, ok = '', error.message, False
            try:
                self.set_note(*command_note(ok, out or '', err or '', done))
            except Exception as error:  # noqa: BLE001 - e.g. output in an unexpected shape
                traceback.print_exc()
                self.set_note(f'{argv[0]} finished, but its output could not be read: {error}', NOTE_SECONDS)
            if ok:  # Settings changed: re-read them (and the rows) shortly.
                GLib.timeout_add(800, lambda: (self.refresh_status(), self.window.get_visible() and self.reload())
                                 and False)
        process.communicate_utf8_async(None, None, finished)

    def set_note(self, text, seconds=None):
        """Under the banner until the next command, or for `seconds`."""
        self.note, self.note_until = text or '', time.monotonic() + seconds if seconds else None
        self.refresh_status()

    def refresh_status(self):
        """The header's status line and the experiments banner, from the files
        the services write; neither may raise (the 2 s timer calls this)."""
        try:
            self.status.set_text(status_line(read_estimate(RUNTIME / 'eta.json'), read_battery()))
        except Exception:  # noqa: BLE001 - one bad line must not stop the rest
            traceback.print_exc()
            self.status.set_text('')
        try:
            self.refresh_experiments()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def refresh_experiments(self):
        state = self.experiment_state()
        status, auto = state.get('status'), state.get('mode') == 'auto'
        enabled = experiment.ENABLED.exists()
        self.syncing = True  # Reflect the files without the widgets acting on it.
        try:
            self.auto_switch.set_active(enabled)
        finally:
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
        if self.note_until is not None and time.monotonic() > self.note_until:
            self.note, self.note_until = '', None
        self.banner.set_text(banner_text(state, enabled, frozen=frozen, quarantine=(self.result or {}).get('quarantine'))
                             + (f'\n{self.note}' if self.note else ''))


CSS = b"""
window { background: transparent; }
#battery-card { background: @menu_bg; color: @menu_fg; border: 1px solid @menu_border;
                border-left-color: @menu_border_strong; border-radius: 12px;
                padding: 12px 14px; font-family: sans-serif; font-size: 13px; }
#battery-card .muted { color: @menu_muted; font-size: 12px; }
#battery-card .problem { color: #f0a35e; font-size: 12px; }
#battery-card .health { font-size: 11px; }
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
