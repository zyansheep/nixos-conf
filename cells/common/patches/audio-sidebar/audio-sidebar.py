#!/usr/bin/env python3
"""Small layer-shell mixer. PipeWire's Pulse server owns all audio state."""
import concurrent.futures
import json
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Gtk4LayerShell as GtkLayerShell, Pango


def pactl(*args):
    result = subprocess.run(["pactl", *map(str, args)], capture_output=True,
                            text=True, timeout=4, check=True)
    return result.stdout


def snapshot():
    state = {kind: json.loads(pactl("-f", "json", "list", kind))
             for kind in ("sinks", "sources", "sink-inputs", "source-outputs")}
    state["default-sink"] = pactl("get-default-sink").strip()
    state["default-source"] = pactl("get-default-source").strip()
    return state


def volume(item):
    channels = item.get("volume", {}).values()
    values = [v["value"] / 65536 * 100 for v in channels if "value" in v]
    return round(sum(values) / len(values)) if values else 0


def description(item):
    props = item.get("properties", {})
    return item.get("description") or props.get("application.name") or props.get("media.name") or item.get("name", "Audio stream")


def is_monitor(item):
    return (item.get("properties", {}).get("device.class") == "monitor"
            or item.get("monitor_of_sink") not in (None, 4294967295)
            or item.get("name", "").endswith(".monitor"))


def topology(state):
    return tuple((kind, tuple((i["index"], i.get("name"), description(i),
                              tuple((p["name"], p.get("description"), p.get("availability"))
                                    for p in i.get("ports", []))) for i in state[kind]))
                 for kind in ("sinks", "sources", "sink-inputs", "source-outputs"))


CSS = b"""
window { background: transparent; }
#audio-card { background: @menu_bg; color: @menu_fg; border: 1px solid @menu_border;
              border-left-color: @menu_border_strong; border-radius: 12px;
              padding: 12px; font-family: sans-serif; font-size: 14px; }
#audio-card .section { font-size: 13px; font-weight: 700; margin-top: 8px; }
#audio-card .device { background: @menu_card; border-radius: 12px; padding: 12px; }
#audio-card .device.active { background: @menu_active; }
#audio-card .muted { color: @menu_muted; font-size: 12px; }
#audio-card button:hover { background: @menu_hover; }
#audio-card button:checked { background: @menu_active; color: @menu_accent; }
"""


class AudioPopup(Adw.Application):
    def __init__(self):
        super().__init__(application_id="org.zyansheep.AudioSidebar",
                         flags=Gio.ApplicationFlags.IS_SERVICE)
        self.window = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending = 0
        self.updating = False
        self.dragging = False
        self.combos = []
        self.updaters = []
        self.state = None
        self.shape = None
        self.timer = None
        self.focused_once = False
        self.volume_timers = {}
        self.connect("startup", self.startup)
        self.connect("activate", self.toggle)
        self.connect("shutdown", self.shutdown)

    def shutdown(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
        for timer in self.volume_timers.values():
            GLib.source_remove(timer)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def startup(self, *_):
        self.hold()
        Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        toggle = Gio.SimpleAction.new("toggle", None)
        toggle.connect("activate", self.toggle)
        self.add_action(toggle)
        self.build_window()
        self.window.realize()
        # Populate hidden widgets once, before the first click.
        self.refresh(force=True)
        self.timer = GLib.timeout_add_seconds(2, self.refresh)

    def toggle(self, *_):
        if self.window.get_visible():
            self.close()
        else:
            self.focus_for_open()
            self.window.present()
            self.refresh()

    def focus_for_open(self):
        self.focused_once = False
        GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.ON_DEMAND)

    def focus_changed(self, window, *_):
        if not window.get_visible():
            return
        if window.is_active():
            self.focused_once = True
            GtkLayerShell.set_keyboard_mode(window, GtkLayerShell.KeyboardMode.ON_DEMAND)
        elif self.focused_once:
            # A dropdown may briefly take keyboard focus within our application.
            GLib.timeout_add(100, self.dismiss_if_unfocused)

    def dismiss_if_unfocused(self):
        if self.window.get_visible() and not self.window.is_active() and not self.busy():
            self.close()
        return False

    def build_window(self):
        self.window = Adw.ApplicationWindow(application=self, title="Audio")
        self.window.connect("close-request", self.close)
        self.window.connect("notify::is-active", self.focus_changed)
        self.window.set_decorated(False)
        self.window.set_default_size(1, 1)
        GtkLayerShell.init_for_window(self.window)
        GtkLayerShell.set_namespace(self.window, "audio-sidebar")
        GtkLayerShell.set_layer(self.window, GtkLayerShell.Layer.OVERLAY)
        # Only the panel is a surface. No backdrop or exclusive keyboard grab:
        # pointer, wheel and touch events outside it go straight to other apps.
        GtkLayerShell.set_keyboard_mode(self.window, GtkLayerShell.KeyboardMode.ON_DEMAND)
        GtkLayerShell.set_exclusive_zone(self.window, -1)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.window, GtkLayerShell.Edge.RIGHT, True)
        monitors = Gdk.Display.get_default().get_monitors()
        monitor = monitors.get_item(0)
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.TOP, 32)
        GtkLayerShell.set_margin(self.window, GtkLayerShell.Edge.RIGHT, 8)
        css = Gtk.CssProvider()
        palette = Path(__file__).resolve().parent.parent / "share/audio-sidebar/menu-theme.css"
        css.load_from_data(palette.read_bytes() + CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_press)
        self.window.add_controller(keys)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.card = card
        card.set_name("audio-card")
        card.set_size_request(400, -1)
        header = Gtk.Box(spacing=8)
        title = self.label("Audio", "title-2")
        title.set_hexpand(True)
        header.append(title)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.add_css_class("flat")
        close.update_property([Gtk.AccessibleProperty.LABEL], ["Close audio"])
        close.connect("clicked", self.close)
        header.append(close)
        card.append(header)
        self.error = self.label("", "muted")
        self.error.set_wrap(True)
        self.error.set_max_width_chars(40)
        self.error.set_visible(False)
        card.append(self.error)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll = Gtk.ScrolledWindow()
        self.scroll = scroll
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        height = monitor.get_geometry().height if monitor else 800
        self.max_content_height = max(180, min(650, height - 140))
        scroll.set_max_content_height(self.max_content_height)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(self.content)
        card.append(scroll)
        actions = Gtk.Box(spacing=8)
        graph = self.button("Routing graph…", self.graph)
        graph.set_hexpand(True)
        actions.append(graph)
        actions.append(self.button("Close", self.close))
        card.append(actions)
        self.window.set_content(card)
        self.fit_panel()

    def fit_panel(self):
        width = self.card.measure(Gtk.Orientation.HORIZONTAL, -1).natural
        height = self.card.measure(Gtk.Orientation.VERTICAL, width).natural
        self.window.set_default_size(width, height)

    def close(self, *_):
        self.window.set_visible(False)
        self.dragging = False
        return True

    def key_press(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            return self.close()
        return False

    def graph(self):
        try:
            subprocess.Popen(["helvum"], start_new_session=True)
            self.close()
        except OSError as error:
            self.error.set_text(str(error))
            self.error.show()

    @staticmethod
    def label(text, style=None):
        label = Gtk.Label(label=text, xalign=0)
        if style:
            label.add_css_class(style)
        return label

    @staticmethod
    def button(text, callback):
        button = Gtk.Button(label=text)
        button.connect("clicked", lambda *_: callback())
        return button

    def busy(self):
        return self.dragging or bool(self.volume_timers) or any(c.get_property("popup-shown") for c in self.combos)

    def refresh(self, force=False):
        if self.window and (force or self.window.get_visible()) and not self.pending and not self.busy():
            self.pending += 1
            self.pool.submit(self.load)
        return True

    def load(self, command=None):
        error = None
        state = None
        try:
            if command:
                pactl(*command)
            state = snapshot()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            error = getattr(exc, "stderr", None) or str(exc)
        GLib.idle_add(self.loaded, state, error)

    def command(self, *args):
        if not self.updating:
            self.pending += 1
            self.pool.submit(self.load, args)

    def loaded(self, state, error):
        self.pending -= 1
        if not self.window:
            return False
        if error:
            self.error.set_text("Audio unavailable: " + error.strip())
            self.error.show()
            return False
        self.error.set_text("")
        self.error.hide()
        if self.busy():
            return False
        self.updating = True
        try:
            self.state = state
            shape = topology(state)
            if shape != self.shape:
                self.shape = shape
                self.rebuild()
            for update in self.updaters:
                update(state)
        finally:
            self.updating = False
        return False

    def combo(self, options, callback):
        combo = Gtk.ComboBoxText()
        for key, name in options:
            combo.append(str(key), name)
        # Prevent unusually long Bluetooth/application names widening the popup.
        for renderer in combo.get_cells():
            if isinstance(renderer, Gtk.CellRendererText):
                renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
                renderer.set_property("max-width-chars", 35)
        combo.connect("changed", lambda c: callback(c.get_active_id()) if c.get_active_id() is not None and not self.updating else None)
        self.combos.append(combo)
        return combo

    def rebuild(self):
        while child := self.content.get_first_child():
            self.content.remove(child)
        self.updaters = []
        self.combos = []
        for kind, heading, singular in (("sinks", "Outputs", "sink"), ("sources", "Inputs", "source"),
                                        ("sink-inputs", "Playback apps", "sink-input"),
                                        ("source-outputs", "Recording apps", "source-output")):
            self.content.append(self.label(heading, "section"))
            items = self.state[kind]
            if kind == "sources":
                items = [i for i in items if not is_monitor(i)]
            if not items:
                self.content.append(self.label("No active apps" if "apps" in heading else "No devices", "muted"))
            for item in items:
                self.add_item(kind, singular, item)


        height = self.content.measure(Gtk.Orientation.VERTICAL, 398).natural
        self.scroll.set_min_content_height(min(height, self.max_content_height))
        self.fit_panel()

    def add_item(self, kind, singular, item):
        index = item["index"]
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("device")
        name = self.label(description(item))
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(38)
        box.append(name)
        row = Gtk.Box(spacing=8)
        mute = Gtk.ToggleButton()
        mute.add_css_class("flat")
        recording = kind in ("sources", "source-outputs")
        mute.connect("toggled", lambda b: self.command("set-" + singular + "-mute", index, "1" if b.get_active() else "0"))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_digits(0)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_hexpand(True)
        # Apply once on release, while keyboard changes apply immediately.
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self.start_drag)
        gesture.connect("released", lambda *_: self.end_drag(singular, index, scale))
        gesture.connect("cancel", lambda *_: setattr(self, "dragging", False))
        scale.add_controller(gesture)
        scale.connect("value-changed", lambda s: self.queue_volume(singular, index, s))
        scale.set_sensitive(bool(item.get("volume")))
        row.append(scale)
        row.append(mute)
        box.append(row)
        default = None
        route = None
        port = None
        if kind in ("sinks", "sources"):
            default = Gtk.CheckButton(label="Default device for new streams")
            default.connect("toggled", lambda b: self.command("set-default-" + singular, item["name"]) if b.get_active() else None)
            box.append(default)
            ports = [(p["name"], p.get("description", p["name"])) for p in item.get("ports", []) if p.get("availability") != "not available"]
            if len(ports) > 1:
                port = self.combo(ports, lambda value: self.command("set-" + singular + "-port", item["name"], value))
                box.append(port)
        else:
            target = "sink" if kind == "sink-inputs" else "source"
            route = self.combo([(i["index"], description(i)) for i in self.state[target + "s"]],
                               lambda value: self.command("move-" + singular, index, value))
            box.append(route)

        def update(state):
            current = next(i for i in state[kind] if i["index"] == index)
            scale.set_value(volume(current))
            muted = current.get("mute", False)
            mute.set_active(muted)
            mute.set_icon_name(("microphone-disabled-symbolic" if muted else "microphone-sensitivity-high-symbolic")
                               if recording else ("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"))
            mute.update_property([Gtk.AccessibleProperty.LABEL],
                                 [("Unmute " if muted else "Mute ") + description(current)])
            if default:
                selected = state["default-" + singular] == current["name"]
                (box.add_css_class if selected else box.remove_css_class)("active")
                default.set_active(selected)
                default.set_sensitive(not selected)
            if route:
                route.set_active_id(str(current["sink" if kind == "sink-inputs" else "source"]))
            if port:
                port.set_active_id(current.get("active_port", ""))
        self.updaters.append(update)
        self.content.append(box)

    def queue_volume(self, singular, index, scale):
        if self.updating or self.dragging:
            return
        key = (singular, index)
        if key in self.volume_timers:
            GLib.source_remove(self.volume_timers[key])
        value = f"{scale.get_value():.0f}%"
        def apply():
            self.volume_timers.pop(key, None)
            self.command("set-" + singular + "-volume", index, value)
            return False
        self.volume_timers[key] = GLib.timeout_add(120, apply)

    def start_drag(self, *_):
        self.dragging = True
        return False

    def end_drag(self, singular, index, scale):
        self.dragging = False
        self.command("set-" + singular + "-volume", index, f"{scale.get_value():.0f}%")
        return False


if __name__ == "__main__":
    raise SystemExit(AudioPopup().run([]))
