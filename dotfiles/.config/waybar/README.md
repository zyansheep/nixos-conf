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
| Sun / moon | Scroll to adjust brightness; click for the display panel (brightness, night light with intensity, grayscale). The moon means the night light is on. |
| Tray chevron | Hover to expand; move away to collapse. |
| Idle inhibitor | Click to toggle whether the screen may sleep. |

The battery outline shows the estimated time left, ± half its 80% interval and
live wattage (for example `1:17 ±0:15 14.2W`), plus the active profile. While
charging it shows a bolt and the time to the charge limit instead; a plug means
connected without charging (“held at limit” at the charge limit). The fill is
remaining *energy* at 1% resolution, not the EC's charge percentage: the gauge
counts charge linearly, but loaded voltage falls from ~17 V near full to ~14 V
near empty, so the bottom percents hold ~15% less energy and drain faster at the
same draw. The outline turns red below 30 minutes or 10% energy and green while
charging. A green leaf means Power saver, blue scales mean Balanced, and an
amber speedometer means Performance. Open the power selector for battery health,
cycle count and the active profile.

`battery-eta` (waybar-monitor) produces the label. Time to empty is remaining
energy (∫ loaded voltage × charge, from the power log's voltage curve) divided
by the forecast average draw over the remaining time, solved jointly. Draw is
modelled as an Ornstein–Uhlenbeck process on minute means (long-run mean,
variance and decay time ≈15 min fitted from history; the recent 10-minute draw
decays toward the mean), giving a log-normal 80% interval that also covers
uncertainty in the long-run mean; current readings are scaled by the charge
counter (~5% under-read). Backtested, the 80% interval for the average draw
covered 82–88% of outcomes at 10–90 minute horizons. Time to full integrates the
learned minutes-per-percent charge curve (constant current, then taper above
~80%), scaled by the last five minutes' rate; its interval is the spread of
10-minute charge rates around that curve. Models refit every 15 minutes.

The profile popup centers its three buttons beneath the battery. A highlighted button
marks the active profile. Battery health and cycle count appear below the
buttons and refresh whenever the menu opens. Health is full capacity divided by design
capacity; unsupported readings show “unavailable”, and zero cycles is valid.
Below that, the time-left distribution: battery-eta's full forecast density
(the battery is empty by t exactly when the average draw over t reaches the
remaining energy / t, so P(T ≤ t) comes straight from the draw forecast), with
the 80% interval shaded darker and the median marked; green and to the charge
limit while charging. Below that, battery use over the last 30 minutes on battery ranks apps and
hardware together in average watts and share, with the size of the long-term
power log underneath (see [Battery use and the power log](#battery-use-and-the-power-log)).
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
is over its indicator or contents, supports scrolling with a visible scrollbar,
preserves the scroll position across refreshes, and closes 180 ms after leaving both. It does not grab background input or re-enable the
other bar tooltips.

- **CPU:** up to 100 program names ranked by rolling past-minute CPU usage,
  normalized to the whole CPU (all cores busy = 100%). The label shows actual
  coverage during the first minute. CPU from already-observed exited programs
  remains in the rolling window until it ages out.
- **Memory:** up to 100 program names by current summed resident memory (RSS).
  Shared pages can be counted in several processes; this is not unique memory
  ownership and will not necessarily sum to the bar's used-memory figure.
- **Temperature:** every readable hwmon temperature channel and thermal zone,
  with chip/channel names. Different interfaces can report the same sensor.

`waybar-monitor.service` samples every two seconds and publishes an atomic cache
under `$XDG_RUNTIME_DIR/waybar-monitor`. It stores no command lines; its only
persistent history is the power log below. CPU counters use PID plus process
start time, so PID reuse cannot inherit another process's CPU time. A long
sampling gap resets the window. Processes that finish entirely between samples
cannot be measured. The service has no filesystem sandbox: in a user manager
`ProtectSystem`/`ProtectHome` imply a private user namespace, which hides the
session's `/proc/PID/fd`, `fdinfo`, `io` and `exe`.

### Battery use and the power log

Linux has no per-process power meter, but this laptop measures two totals:
battery power (`current_now × voltage_now`) and whole-chip power (amdgpu's
`PPT`, CPU + GPU). Both are sampled four times a second. The menu splits them:

- **Chip power → apps.** Above a learned floor (“Processor baseline”), chip
  power is divided by each app's load and GPU/video engine time (DRM `fdinfo`,
  deduplicated by client id). Load is busy CPU time × clock² (GHz): dynamic
  power grows with frequency and voltage, and busy time alone explained only
  45% of chip power versus 73% with the clock term. Busy CPU time not owned by a
  process goes to “Kernel”.
- **Chip watts cost more than a watt of battery.** On one-minute means,
  battery ≈ 0.3 + 1.39 × chip + 3.0 × backlight W (R² 0.945): conversion losses
  and rails outside PPT follow chip load. On battery, app and baseline shares
  are multiplied by that learned factor.
- **Battery − k × chip → hardware.** Display (backlight), Wi-Fi (traffic),
  storage (busy time), USB devices, audio and keyboard backlight use learned
  watts per unit of activity; the unexplained remainder is “Rest of system”.

Both are ridge regressions updated online, shrunk toward prior guesses so
features that never vary keep plausible values, and fitted on battery time only
(AC runs a hungrier power profile). The platform model sees one-minute means:
the battery reading lags chip power by ~10 s. Coefficients persist in
`models.json`. On AC the battery reading is charge, not consumption, so the
menu shows the last 30 minutes on battery or, failing that, chip power only.

Every 10 seconds the monitor appends one JSON line of **raw inputs** (not
attributions) to `~/.local/state/waybar-monitor/power/YYYY-MM-DD.jsonl`; finished
days become `.jsonl.zst`. Records hold measurements for offline model fitting
and counterfactuals (“what if this app were closed / this setting changed”):

| Key | Contents |
| --- | --- |
| `t0`, `t1`, `dt`, `boot` | wall-clock interval, its boottime length, boot id prefix |
| `ac`, `status`, `bat` | mains, battery status; mean/min/max signed W (discharge positive), %, charge, volts |
| `soc`, `gpu` | chip W (mean/min/max); GPU busy %, summed GPU/video engine seconds |
| `cpu` | user/sys/irq/iowait/idle seconds, `busy_s`, mean/max MHz, C-state residency, `ctxt`, `intr` |
| `irq` | top interrupt sources by count |
| `apps` | per coarse app: processes `n`, `rss` MiB, `cpu`/`gpu`/`video` seconds, `io_r`/`io_w` bytes |
| `net`, `wifi_dbm`, `disk` | per-interface byte/packet deltas; signal; per-disk bytes and busy seconds |
| `usb`, `pci`, `audio` | runtime-active USB devices and PCI drivers; running PCM streams |
| `display` | backlight and keyboard fractions, connectors, ABM level, niri modes, display-panel state |
| `settings` | platform profile, EPP, governor, boost, pstate, ASPM, NVMe APST, charge limit, rfkill, Wi-Fi power save |
| `temp`, `fan`, `lid`, `locked` | Tctl, fan RPM, lid state, swaylock running |

Records also carry `exp` (run, experiment, block, arm, value, washout) while a
power experiment is running.

Events share the files: `start` (collector start) and `gap` (suspend or other
sampling gap, with battery charge before and after, for sleep drain).
`index.json` caches per-day record counts for the menu footer. App names are
coarse (kernel threads are `Kernel`; no PIDs, command lines, browser origins or
titles). Expect roughly 40 MB/day uncompressed; read with e.g.
`duckdb -c "select * from read_json('~/.local/state/waybar-monitor/power/*.jsonl*')"`.

### Battery panel and power experiments

“History, what-if & experiments…” at the bottom of the battery menu (or
`battery-panel [timeline|sleep|whatif|experiments]`) toggles a resident
layer-shell panel. One row of controls scopes everything: 6 h / Day / Week,
back / forward (or ← →), Now. Tabs:

- **Timeline** — stacked average watts per moment by group (processor
  baseline, rest of system, display, and stable app groups: browser, builds &
  EDA, coding tools, chat & media, other apps, desktop & system), with a
  separate battery-% strip. Faded columns are on AC (chip power only); shaded
  bands are sleep. Hover lists the groups, the top apps and the battery level.
- **Sleep drain** — one dot per suspend (size = length) at %/h, with the median
  across all history.
- **What-if runtime** — extra minutes per full charge, with 90% intervals.
  Model estimates (outlined) remove an app's or group's activity, or dim the
  display, and refit on 10-minute moving-block bootstrap resamples (residuals
  stay correlated for minutes, so naive intervals would be ~3× too narrow).
  Experiment results (solid) come from paired A/B blocks with a pair bootstrap.
  Runtime = full-charge Wh ÷ typical draw, from this period or all history.
- **Experiments** — randomized switchback tests: `power-experiment.service`
  alternates a setting between A (current) and B in random-order 4-minute
  blocks while on battery, discards each block's first minute, pauses (and
  restores A) on AC, and restores A on stop or crash. Settings: ASPM, NVMe APST,
  CPU boost, panel ABM, power profile, refresh rate, Wi-Fi power save, and
  freezing an app scope (`systemctl --user freeze`, optionally only while the
  screen is locked). Root settings go through `sudo -n power-lab`; power-lab's
  `sleep-safe` pre-sleep hook restores the boot-time ASPM policy and NVMe APST
  limit (the s2idle crash workarounds) before every suspend, and the runner
  starts a fresh block after resume. Profile and ABM are left to the AC udev
  rules while plugged in.

`report.py` caches per-minute aggregates in `~/.cache/battery-panel` (finished
days whole, today up to its last complete minute). `battery_panel.py --render
DIR [6 h|Day|Week]` writes the three charts as PNGs without a window.

### Browser processes and readable labels

Process rows use application names and roles where the executable or installed
Electron entry point identifies them. Browser web-content processes are split
into individual PID rows (for example, `Floorp web · PID 12345`) instead of
combining every `Isolated Web Co` process into one large total. Helpers have
labels such as `Floorp · extensions` and `Floorp · media decoder`.

Floorp's packaged AutoConfig bridge calls `ChromeUtils.requestProcInfo()` every
five seconds after startup. It writes origins and up to three document titles
per process to `$XDG_RUNTIME_DIR/floorp-monitor/<browser-pid>.json` (directory
0700, file 0600). Private-browsing origins are excluded. Full page URLs and
persistent browsing history are not exported. No remote debugging port is used.
The package enables privileged AutoConfig for this script; the web-content
sandbox remains enabled. Floorp must be restarted after installing the bridge.

The collector joins these labels using PID **and process start time**, ignores
snapshots older than 15 seconds, and decorates rows only after calculating CPU
usage. Changing titles cannot split the rolling CPU history. Browser failures
fall back to PID labels. This is an internal Firefox API and may need maintenance
when Floorp changes. One process can contain multiple tabs, and cross-site frames
can run in separate processes: these remain process totals, not exact per-tab
measurements. `about:processes` provides the browser's full breakdown.

Limited Electron role/entry-point arguments are read transiently; full command
lines are never written to the cache.

## Notification panel

SwayNC supplies notification grouping, actions, inline replies, DND and media
controls. `swaync.service` now pins the configured GTK4 package and preloads its
widgets/surface at graphical-session startup; `swaync-warm-renderer.patch` also
realizes the hidden panel so the first `Alt+N` is not slowed by Vulkan setup. The panel has no opening
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
fullscreen invisible click catcher and no exclusive keyboard grab. Scroll a
background application without closing the panel. Niri gives an on-demand panel
keyboard focus when it opens, so clicking another window moves focus away and
dismisses Audio and Wi-Fi; dropdown popovers
and in-panel dialogs keep focus. Clicking Waybar itself does not take focus and
leaves the panel open.

Escape closes the innermost layer first: an open dropdown or dialog, then a
details page, then the panel. Use Close or `Alt+Shift+V` to dismiss audio. Wi-Fi
toggles with `Alt+Shift+W`; notifications toggle with `Alt+N`.

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

The battery and profile modules sit inside `group/power`, sharing a 104px outline
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
windows and on-demand keyboard focus. The Wi-Fi fork (`cells/common/vendor/nm-sidebar`)
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
capacity/cycle fallbacks against synthetic sysfs fixtures and the battery-use snapshot parser. Apply using `nrb` to
install the commands, CSS link, and patched packages. Niri reloads linked key
bindings. `Alt+Shift+R` restarts Waybar and notifications using the system package;
the managed Wi-Fi service is replaced by a rebuild.

## Power history research

See [power tracing feasibility](../../../cells/nixos/hosts/isomorph/power-tracing.md)
for available hardware counters, existing tools, and a proposed battery history
view. Continuous tracing is not enabled by these menu changes.

## Display panel

`display-panel` (package `cells/common/packages/display-panel.nix`, source in
`cells/common/patches/display-panel`) is a resident GTK4 popup like the audio one.
Brightness goes through `brightnessctl`. The night light sets the color
temperature of `wl-gammarelay-rs` over D-Bus: 6500 K when off, down to 2500 K at
100% intensity. Grayscale maps a full-screen overlay in the `grayscale-filter`
namespace with an empty input region. A niri layer rule in `config.kdl` desaturates
everything behind it with a non-xray background effect. That effect is experimental
in niri 26.04: color briefly returns during window open/close animations and while
dragging tiled windows, and the whole screen is re-rendered off-screen while it is
on. The overlay draws a 1/255-alpha background because GTK skips frames for a fully
transparent window, and niri needs a committed buffer to apply the effect.
Modes persist in `~/.local/state/display-panel/state.json` and are restored at login;
the panel re-applies the temperature whenever the gamma daemon restarts.
