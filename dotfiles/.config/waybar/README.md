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

## Audio popup

The speaker and microphone open `audio-sidebar`, also bound to `Alt+Shift+V`.
It uses plain GTK 4 and gtk4-layer-shell, with a compact custom style and no
libadwaita header. A session service preloads GTK and constructs the window in
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
focus the panel when keyboard input is needed.

Use Close, Escape while focused, or `Alt+Shift+V` to dismiss audio. Wi-Fi toggles
with `Alt+Shift+W`; notifications toggle with `Alt+N`.

The battery intentionally uses a native GTK 3 menu inside Waybar again. This
restores press-drag-release selection and avoids starting another process. It
closes on selection, Escape, or clicking outside. Its normal menu grab means
background scrolling is unavailable until it closes.

## Menu style

[menu-theme.css](menu-theme.css) provides the GTK4 palette for audio and SwayNC,
matching Wi-Fi's dark palette. Audio uses compact custom widgets without a
libadwaita title bar. The native battery menu has its compact styles in
[style.css](style.css), with three profile choices and a health/cycles footer.
Wi-Fi and notifications remain GTK4/libadwaita; battery is GTK3 inside Waybar.

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
  path:.#nixosConfigurations.isomorph.config.programs.waybar.package --no-link
```

The audio package tests command handling, hotplug behavior, monitor-source
classification, and resident-window behavior. The Waybar build tests battery
capacity/cycle fallbacks against synthetic sysfs fixtures. Apply using `nrb` to
install the commands, CSS link, and patched packages. Niri reloads linked key
bindings. `Alt+Shift+R` restarts Waybar and notifications using the system package;
the managed Wi-Fi service is replaced by a rebuild.

## Power history research

See [power tracing feasibility](../../../cells/nixos/hosts/isomorph/power-tracing.md)
for available hardware counters, existing tools, and a proposed battery history
view. Continuous tracing is not enabled by these menu changes.
