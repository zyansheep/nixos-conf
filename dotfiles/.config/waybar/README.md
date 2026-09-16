# Waybar

The Niri bar is configured in [config.jsonc](config.jsonc) and
[style.css](style.css). System integration lives in the
[Niri profile](../../../cells/nixos/profiles/graphics/niri.nix).

## Using the bar

| Control | Interaction |
| --- | --- |
| CPU / memory dials | Click to switch between the icon and usage number. |
| Speaker / microphone dials | Scroll to adjust that device; right-click to mute; click for the audio popup. `Alt+Shift+V` toggles the same popup. |
| Wi-Fi name or icon | Click to open the network sidebar. `Alt+Shift+W` opens the same sidebar. |
| Battery | Click either half to open the centered power profile selector with battery health and cycle count. |
| Tray chevron | Hover to expand; move away to collapse. |
| Idle inhibitor | Click to toggle whether the screen may sleep. |

The battery contains its percentage, charging mark, and active profile in one
outline. A green leaf means Power saver, blue scales mean Balanced, and an amber
speedometer means Performance. Below 20% the battery turns red; charging makes
it green with a bolt. A plug means connected to power without charging, such as
when a charge limit is reached. Open the power selector for battery health,
cycle count and the active profile.

The profile popup centers its three buttons beneath the battery. A highlighted button
marks the active profile. Battery health and cycle count appear below the
buttons and refresh whenever the menu opens. Health is full capacity divided by design
capacity; unsupported readings show “unavailable”, and zero cycles is valid.
AC plug/unplug events still select Balanced / Power saver through udev rules.

Wi-Fi lights another curved bar at 25%, 50%, and 75%; unlit bars stay faintly
visible. Disconnected or disabled Wi-Fi has a muted crossed-out icon. Open the
Wi-Fi sidebar for network details. See
[networking and connection storage](../../../cells/nixos/hosts/isomorph/README.md)
for the sidebar and VPN workflow.

Hover tooltips are disabled across the top bar, including the tray, so they do
not cover open menus. Module settings disable them immediately on reload; the
Waybar package also disables GTK tooltip windows for tray items. Audio device
names and routing controls use visible labels without extra hover hints.

## CPU, memory and temperature hover panels

Hovering any of these indicators opens a native GTK3 panel immediately, without
starting a process or waiting to sample. The panel stays open while the pointer
is over its indicator or contents, supports scrolling its list, and closes
180 ms after leaving both. It does not grab background input or re-enable the
other bar tooltips.

- **CPU:** top 20 program names ranked by rolling past-minute CPU usage,
  normalized to the whole CPU (all cores busy = 100%). The label shows actual
  coverage during the first minute. CPU from already-observed exited programs
  remains in the rolling window until it ages out.
- **Memory:** top 20 program names by current summed resident memory (RSS).
  Shared pages can be counted in several processes; this is not unique memory
  ownership and will not necessarily sum to the bar's used-memory figure.
- **Temperature:** every readable hwmon temperature channel and thermal zone,
  with chip/channel names. Different interfaces can report the same sensor.

`waybar-monitor.service` samples every two seconds and publishes an atomic cache
under `$XDG_RUNTIME_DIR/waybar-monitor`. It stores no command lines or persistent
history. CPU counters use PID plus process start time, so PID reuse cannot
inherit another process's CPU time. A long sampling gap resets the window.
Processes that finish entirely between samples cannot be measured.

## Notification panel

SwayNC supplies notification grouping, actions, inline replies, DND and media
controls. `swaync.service` now pins the configured GTK4 package and preloads its
widgets/surface at graphical-session startup. The panel has no opening
transition. `Alt+N` toggles it; `Alt+Shift+R` restarts the managed Waybar and
notification services. Broken legacy Wi-Fi/Bluetooth/DPMS quick-toggle shell
commands were removed; their dedicated panels remain available.

## Audio popup

The speaker and microphone open `audio-sidebar`, also bound to `Alt+Shift+V`.
It uses GTK 4, libadwaita and gtk4-layer-shell, with the same rounded cards,
spacing, title row and dark palette as Wi-Fi. Mute buttons show speaker or
microphone icons and expose accessible action labels. A session service preloads GTK and constructs the window in
the background; the launcher sends a D-Bus toggle action to that process.

- Outputs and inputs: volume (0–100%), mute, default device, and available ports.
- Playback and recording apps: individual levels, mute, and a device routing
  dropdown. Recording routes include output monitors for capturing system audio.
- “Routing graph…” opens Helvum for arbitrary PipeWire port connections.

Default-device changes select the fallback for new streams; use each app's
routing dropdown to explicitly move an existing stream. Native PipeWire clients
that do not appear through the PulseAudio protocol are available in Helvum.
The popup refreshes while open and preserves controls during interaction.
Dismissal hides the window for reuse; audio-server polling pauses while hidden. Errors from the audio server appear inside the popup.

### Background interaction

Audio, Wi-Fi and notifications occupy only their panel's bounds. There is no
fullscreen invisible click catcher and no exclusive keyboard grab. Scroll or
click a background application without closing the panel. Click a control to
focus the panel when keyboard input is needed. Audio and notifications dismiss
after losing that focus to another application. On Niri, a panel opened without
first receiving focus cannot detect a click in the already-focused background
app; use its toggle or Close control in that case. Full click-outside dismissal
would require a pointer grab or backdrop that also blocks background scrolling.

Use Close, Escape while focused, or `Alt+Shift+V` to dismiss audio. Wi-Fi toggles
with `Alt+Shift+W`; notifications toggle with `Alt+N`.

The battery intentionally uses a native GTK 3 menu inside Waybar again. This
restores press-drag-release selection and avoids starting another process. It
closes on selection, Escape, or clicking outside. Its normal menu grab means
background scrolling is unavailable until it closes.

## Menu style

[menu-theme.css](menu-theme.css) provides the GTK4 palette for audio and SwayNC,
matching Wi-Fi's dark palette. Audio uses the same libadwaita controls and a simple title row as Wi-Fi. The native battery menu has its compact styles in
[style.css](style.css), with three profile choices and a health/cycles footer.
Audio, Wi-Fi and notifications use GTK4/libadwaita; battery and resource hover
panels use GTK3 inside Waybar.

Rebuild after palette changes to update the audio package. Reload notification
styles with `swaync-client --reload-css` after the shared file is installed.

## Artwork and layout

All four dials are 22px and advance in 5% steps, turning amber at 75% and red at
90%. CPU and memory use their native Waybar modules. Speaker and microphone use
separate native WirePlumber modules attached to the default sink and source;
each scrolls in 1% steps, capped at 100%. A muted device retains its volume but
shows a crossed-out icon and an empty gray ring. Wi-Fi uses native signal states
to select its four SVGs. These displays need no additional polling processes.

Icons select the installed `Font Awesome 7 Free` face explicitly. The old
`FontAwesome` family fell back to a patched monospace font whose ink overran its
advance width, causing offsets and overlap. The memory glyph has a 1px optical
correction right/down while its ring keeps the same 22px box. Tray/idle,
idle/Wi-Fi and notification/audio spacing is set in the stylesheet.

The battery and profile modules sit inside `group/power`, sharing a 64px outline
and translucent fill in 10% steps. Three SVG outlines provide normal, low and
charging colors. They are adapted from [Lucide's battery](https://lucide.dev/icons/battery);
the license is in [gauges/LUCIDE-LICENSE](gauges/LUCIDE-LICENSE).

## Changing the configuration

For open arcs instead of full rings, change `name` from `gauges-ring` to
`gauges-arc` in [config.jsonc](config.jsonc).

The checked-in SVGs and marked CSS block are generated by
[scripts/generate-gauges.mjs](scripts/generate-gauges.mjs). Edit the generator,
then run this from the repository root:

```sh
node dotfiles/.config/waybar/scripts/generate-gauges.mjs
```

Node is only needed for regeneration. Home Manager installs individual
out-of-store symlinks via [dotfiles.nix](../../../cells/home/profiles/dotfiles.nix).
Existing configuration and artwork edits can be reloaded with:

```sh
systemctl --user kill --kill-whom=main -s SIGUSR2 waybar.service
```

Rebuild after adding files so Home Manager installs their links. Changes to the
Waybar package or patches also require a NixOS rebuild. The Niri profile tracks
the configuration, stylesheet, artwork directory and profile menu as service
restart triggers.

## Popup implementation

The battery/profile observers draw the grouped icon natively in Waybar.
`waybar-group-menu.patch` mirrors their state classes and anchors the native
menu to the center of the whole group. It passes the original pointer press
through to GTK so dragging into a choice and releasing activates it.
`waybar-power-menu-stats.hpp` reads battery capacity and cycles from sysfs when
the menu opens. The profile patch delegates menu clicks instead of cycling.

The Python popup in `cells/common/patches/audio-sidebar` uses bounded layer-shell
windows and on-demand keyboard focus. `nm-sidebar/background-input.patch`
limits the Wi-Fi surface to the sidebar width. SwayNC uses
`layer-shell-cover-screen: false` and `swaync-background-input.patch` selects
on-demand keyboard input in that mode. Its blank windows stay hidden.

## Verification and activation

Build without switching the system (the path flake includes new files):

```sh
nix build path:.#packages.x86_64-linux.audio-sidebar \
  path:.#packages.x86_64-linux.nm-sidebar \
  path:.#packages.x86_64-linux.notification-sidebar \
  path:.#packages.x86_64-linux.waybar-monitor \
  path:.#nixosConfigurations.isomorph.config.programs.waybar.package --no-link
```

The monitor tests cover rolling CPU windows, PID reuse, exited tasks, memory
ordering and sensor parsing. The audio package tests command handling, hotplug behavior, monitor-source
classification, and resident-window behavior. The Waybar build tests battery
capacity/cycle fallbacks against synthetic sysfs fixtures. Apply using `nrb` to
install the commands, CSS link, and patched packages. Niri reloads linked key
bindings. `Alt+Shift+R` restarts Waybar and notifications using the system package;
the managed Wi-Fi service is replaced by a rebuild.

## Power history research

See [power tracing feasibility](../../../cells/nixos/hosts/isomorph/power-tracing.md)
for available hardware counters, existing tools, and a proposed battery history
view. Continuous tracing is not enabled by these menu changes.
