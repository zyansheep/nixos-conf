#!/usr/bin/env python3
"""Layer-shell display popup: backlight, night light (wl-gammarelay-rs) and grayscale.

Grayscale maps a full-screen, input-less overlay in the "grayscale-filter" namespace;
a niri layer rule desaturates everything behind it (non-xray background effect).
"""
import json
import os
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
import cairo  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Gtk4LayerShell as GtkLayerShell  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "display-panel/state.json"
DEFAULTS = {"night": False, "intensity": 60, "grayscale": False}
NEUTRAL_K, WARMEST_K = 6500, 2500
WAYBAR_SIGNAL = 11  # Waybar's custom/display module refreshes on SIGRTMIN+11.


def load_state(path=STATE):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    state = dict(DEFAULTS)
    state.update({k: v for k, v in data.items() if k in DEFAULTS and type(v) is type(DEFAULTS[k])})
    state["intensity"] = min(100, max(5, state["intensity"]))
    return state


def save_state(state, path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def temperature(state):
    """Color temperature in kelvin: neutral when off, warmer as intensity rises."""
    if not state["night"]:
        return NEUTRAL_K
    return round(NEUTRAL_K - (NEUTRAL_K - WARMEST_K) * state["intensity"] / 100)


def backlight():
    devices = sorted(Path("/sys/class/backlight").glob("*"))
    return devices[0] if devices else None


def brightness_percent(device):
    try:
        current = int((device / "brightness").read_text())
        maximum = int((device / "max_brightness").read_text())
    except (OSError, ValueError, TypeError):
        return None
    return round(100 * current / maximum) if maximum > 0 else None


CSS = b"""
window { background: transparent; }
window#grayscale-overlay { background: rgba(0, 0, 0, 0.004); }
#display-card { background: @menu_bg; color: @menu_fg; border: 1px solid @menu_border;
                border-left-color: @menu_border_strong; border-radius: 12px;
                padding: 12px; font-family: sans-serif; font-size: 14px; }
#display-card .row { background: @menu_card; border-radius: 12px; padding: 10px 12px; }
#display-card .muted { color: @menu_muted; font-size: 12px; }
#display-card button:hover { background: @menu_hover; }
"""


class DisplayPanel(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.zyansheep.DisplayPanel",
                         flags=Gio.ApplicationFlags.IS_SERVICE)
        self.state = load_state()
        self.device = backlight()
        self.window = self.overlay = None
        self.updating = False
        self.focused_once = False
        self.brightness_timer = self.poll_timer = 0
        self.connect("startup", self.startup)
        self.connect("activate", self.toggle)

    def startup(self, *_):
        self.hold()
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        toggle = Gio.SimpleAction.new("toggle", None)
        toggle.connect("activate", self.toggle)
        self.add_action(toggle)
        css = Gtk.CssProvider()
        palette = Path(__file__).resolve().parent.parent / "share/display-panel/menu-theme.css"
        css.load_from_data(palette.read_bytes() + CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.build_window()
        self.build_overlay()
        # Warm the renderer and layout so the first click opens instantly.
        self.window.realize()
        self.window.get_content().measure(Gtk.Orientation.VERTICAL, -1)
        # Re-apply the temperature whenever the gamma daemon (re)appears.
        Gio.bus_watch_name(Gio.BusType.SESSION, "rs.wl-gammarelay", Gio.BusNameWatcherFlags.NONE,
                           lambda *_: self.apply_temperature(), None)
        self.apply_grayscale()
        self.sync_widgets()

    # --- popup -----------------------------------------------------------------
    def build_window(self):
        self.window = Adw.ApplicationWindow(application=self, title="Display")
        self.window.connect("close-request", self.close)
        self.window.connect("notify::is-active", self.focus_changed)
        self.window.set_decorated(False)
        self.window.set_default_size(1, 1)
        GtkLayerShell.init_for_window(self.window)
        GtkLayerShell.set_namespace(self.window, "display-panel")
        GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.ON_DEMAND)
        GtkLayerShell.set_exclusive_zone(self.window, -1)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, 32)
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.RIGHT, 8)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, *_: keyval == Gdk.KEY_Escape and self.close())
        self.window.add_controller(keys)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.set_name("display-card")
        card.set_size_request(340, -1)
        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label="Display", xalign=0, hexpand=True)
        title.add_css_class("title-2")
        header.append(title)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.add_css_class("flat")
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close display settings"])
        close.connect("clicked", self.close)
        header.append(close)
        card.append(header)

        self.brightness = self.slider(0, 100, self.brightness_changed, "%")
        card.append(self.row("display-brightness-symbolic", "Brightness", self.brightness,
                             sensitive=self.device is not None))

        self.night = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.night.connect("notify::active", lambda s, _p: self.set_option("night", s.get_active()))
        self.intensity = self.slider(5, 100, lambda s: self.set_option("intensity", round(s.get_value())), "%")
        self.kelvin = Gtk.Label(xalign=0)
        self.kelvin.add_css_class("muted")
        night = self.row("night-light-symbolic", "Night light", self.night)
        night.append(self.labelled("Intensity", self.intensity))
        night.append(self.kelvin)
        card.append(night)

        self.gray = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.gray.connect("notify::active", lambda s, _p: self.set_option("grayscale", s.get_active()))
        card.append(self.row("color-select-symbolic", "Grayscale", self.gray))
        self.window.set_content(card)
        width = card.measure(Gtk.Orientation.HORIZONTAL, -1).natural
        self.window.set_default_size(width, card.measure(Gtk.Orientation.VERTICAL, width).natural)

    @staticmethod
    def slider(low, high, callback, unit):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 1)
        scale.set_digits(0)
        scale.set_hexpand(True)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_format_value_func(lambda _s, value: f"{value:.0f}{unit}")
        scale.connect("value-changed", callback)
        return scale

    @staticmethod
    def labelled(text, widget):
        box = Gtk.Box(spacing=8)
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("muted")
        box.append(label)
        box.append(widget)
        return box

    @staticmethod
    def row(icon, text, control, sensitive=True):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("row")
        line = Gtk.Box(spacing=10)
        line.append(Gtk.Image.new_from_icon_name(icon))
        line.append(Gtk.Label(label=text, xalign=0, hexpand=not isinstance(control, Gtk.Scale)))
        line.append(control)
        box.append(line)
        box.set_sensitive(sensitive)
        return box

    def toggle(self, *_):
        if self.window.get_visible():
            self.close()
            return
        self.focused_once = False
        GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.ON_DEMAND)
        self.sync_widgets()
        self.window.present()
        # Follow brightness keys while open.
        self.poll_timer = self.poll_timer or GLib.timeout_add(500, self.poll_brightness)

    def close(self, *_):
        self.window.set_visible(False)
        if self.poll_timer:
            GLib.source_remove(self.poll_timer)
            self.poll_timer = 0
        return True

    def focus_changed(self, window, *_):
        if not window.get_visible():
            return
        if window.is_active():
            self.focused_once = True
        elif self.focused_once:
            GLib.timeout_add(100, lambda: (window.get_visible() and not window.is_active()
                                           and self.close()) and False)

    # --- state -----------------------------------------------------------------
    def sync_widgets(self):
        self.updating = True
        try:
            if self.device and not self.brightness_timer:
                percent = brightness_percent(self.device)
                if percent is not None:
                    self.brightness.set_value(percent)
            self.night.set_active(self.state["night"])
            self.intensity.set_value(self.state["intensity"])
            self.intensity.set_sensitive(self.state["night"])
            self.kelvin.set_text(f"Color temperature: {temperature(self.state)} K")
            self.gray.set_active(self.state["grayscale"])
        finally:
            self.updating = False

    def poll_brightness(self):
        if not self.window.get_visible():
            self.poll_timer = 0
            return False
        self.sync_widgets()
        return True

    def set_option(self, key, value):
        if self.updating or self.state[key] == value:
            return
        self.state[key] = value
        try:
            save_state(self.state)
        except OSError:
            pass
        if key == "grayscale":
            self.apply_grayscale()
        else:
            self.apply_temperature()
        self.sync_widgets()
        subprocess.run(["pkill", f"-RTMIN+{WAYBAR_SIGNAL}", "-x", ".waybar-wrapped"], check=False)

    def apply_temperature(self):
        Gio.bus_get_sync(Gio.BusType.SESSION).call(
            "rs.wl-gammarelay", "/",
            "org.freedesktop.DBus.Properties", "Set",
            GLib.Variant("(ssv)", ("rs.wl.gammarelay", "Temperature",
                                   GLib.Variant("q", temperature(self.state)))),
            None, Gio.DBusCallFlags.NONE, 2000, None, None)

    def brightness_changed(self, scale):
        if self.updating:
            return
        if self.brightness_timer:
            GLib.source_remove(self.brightness_timer)

        def apply():
            self.brightness_timer = 0
            subprocess.Popen(["brightnessctl", "-q", "set", f"{scale.get_value():.0f}%"])
            return False
        self.brightness_timer = GLib.timeout_add(60, apply)

    # --- grayscale overlay -------------------------------------------------------
    def build_overlay(self):
        self.overlay = Gtk.Window(application=self, title="Grayscale filter")
        self.overlay.set_name("grayscale-overlay")
        self.overlay.set_decorated(False)
        # GTK skips frames for a fully transparent window, so the background is
        # rgba(0,0,0,1/255) (see CSS); niri needs a committed buffer to apply the effect.
        self.overlay.set_child(Gtk.Box(hexpand=True, vexpand=True, can_target=False))
        GtkLayerShell.init_for_window(self.overlay)
        GtkLayerShell.set_namespace(self.overlay, "grayscale-filter")
        GtkLayerShell.set_layer(self.overlay, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_keyboard_mode(self.overlay, GtkLayerShell.KeyboardMode.NONE)
        GtkLayerShell.set_exclusive_zone(self.overlay, -1)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                     GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self.overlay, edge, True)
        # Empty input region: every click and scroll passes through to the apps below.
        self.overlay.connect("realize", lambda w: w.get_surface().set_input_region(cairo.Region()))

    def apply_grayscale(self):
        self.overlay.set_visible(self.state["grayscale"])


if __name__ == "__main__":
    raise SystemExit(DisplayPanel().run([]))
