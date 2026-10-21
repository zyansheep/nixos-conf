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
| 👍 / 👎 | Vote on what stands out right now. 👍: something is especially good (unexpectedly smooth, quiet, snappy or long-lasting). 👎: worse than it should be — automatic experiments restore your settings at once and rest 10 minutes. Both are logged; they teach the experiments what to keep and what to avoid. Right-click either for the battery panel's Experiments tab. |
| Sun / moon | Scroll to adjust brightness; click for the display panel (brightness, night light with intensity, grayscale). The moon means the night light is on. |
| Tray chevron | Hover to expand; move away to collapse. |
| Idle inhibitor | Click to toggle whether the screen may sleep. |

The battery outline shows the estimated time left, ± half its 80% interval and
live wattage (for example `1:17 ±0:15 14.2W`), plus the active profile. While
charging it shows a bolt, the time on battery banked so far and the time to the
charge limit (`⚡ ~1:42 full ~0:35 24W`; ~ because their ranges are ≈±20% and
≈±30%, shown in the menu with the minutes of use each charging minute adds); a plug means
connected without charging (“held at limit” at the charge limit). The fill is
remaining *energy* at 1% resolution, not the EC's charge percentage: the gauge
counts charge linearly, but loaded voltage falls from ~17 V near full to ~14 V
near empty, so the bottom percents hold ~15% less energy and drain faster at the
same draw. The outline turns red below 30 minutes or 10% energy and green while
charging. A green leaf means Power saver, blue scales mean Balanced, and an
amber speedometer means Performance. Open the power selector for battery health,
cycle count and the active profile.

`drainhound eta` (the drainhound-eta service; the bar runs `drainhound waybar battery`) produces the label. Time left is *awake* time
on battery (how long a suspend would last is a separate estimate, below): the
remaining energy (∫ loaded voltage × charge, from the power log's voltage
curve) divided by the forecast average draw over the remaining time, solved
jointly. The draw is an Ornstein–Uhlenbeck process in log watts (minute
means) that reverts to a *local level* — the typical draw of roughly the last
5 hours on battery — rather than to a month-long mean, so a light day and a
heavy build day each forecast from their own recent use; the recent 10-minute
draw decays toward that level, with the decay time and variance fitted from
history. The forecast is log-normal: its 80% interval also covers the
uncertainty in the level and is calibrated against backtests of past
discharges, and averaging on the log scale is corrected for (Jensen: the mean
of a log-normal draw sits above its median). Current readings are scaled by
the charge counter, which reads a few percent under. (Gains per full charge
use a longer-run draw instead: see the panel's *typical draw*.) Time to full integrates the
learned minutes-per-percent charge curve (constant current, then taper above
~80%), scaled by the last five minutes' rate; its interval is the spread of
10-minute charge rates around that curve. Models refit every 15 minutes.

Empty is where the battery cuts out, not zero charge. Nothing shuts the laptop
down on low battery (UPower isn't running), so the embedded controller cuts
the power, and it does so with charge still on the gauge: five deaths
(2026-10-03 to 10-11) all ended at 2% with 47–59 mAh left at 8–25 W, while
the loaded voltage (13.5–13.9 V) never collapsed. Until then drainhound eta
counted down to zero and, one minute before each death, still showed 3.5–6
minutes. `eta.fit_cutoff` learns the cutoff from the boots the battery cut
below 3% (56 mAh ± 2 here), and time left counts the energy above it. Below
5% the time comes from the gauge itself rather than the watts: its falling
charge over the last 10 minutes (5 at least), extrapolated to the cutoff, and
in the last 3 minutes its slope over the past 2 minutes, which times the
second it cuts out. The gauge lowers its estimate faster under heavy draw (1
mAh/s at 25 W, twice what the current explains), which the watts alone miss.
Backtested leaving each death out: from two minutes before, within ~10 s on
average and 35 s at worst, against 3.5–5 minutes late before. Below 10 minutes the label counts down
(`2m 07s ±18s`; it blinks red for the last 3 minutes, as the power LED does),
the menu and panel say “Cuts out at 10:30:41”, and a notification comes at 5
minutes and at 1. Below 5% a black box fsyncs the last reading every 2 s to
`~/.local/state/drainhound/lastgasp.json`; the next start turns a last
gasp from an earlier boot close to the cutoff into a timed death in
`deaths.json`, which the cutoff fit prefers (the database itself can lose the
last ~5 s of writes, and the raw archive lost 50 s). Deaths found in the
records are remembered there too, so the cutoff outlives the 30-day window.
How each earlier boot ended comes from the system journal: the battery cut it
(no shutdown and no reset reason), it shut down (or began to, and the battery
cut that short), it died asleep, or the next boot gives a reset reason (a sync
flood, a reboot). Only battery cuts teach the cutoff; any other ending is
remembered as not a death, so a crash at 2% can't drag the cutoff up.
deaths.json is written atomically, its previous version kept as
`deaths.json.bak`.

Once a death has taught the cutoff, drainhound eta powers the laptop off cleanly
(`systemctl poweroff`, which logind allows the active session without a
password) instead of letting the power vanish: apps close, the disks settle,
the journal keeps its tail. It aims at the *early* edge of the cutoff
estimate — the soonest the battery could plausibly cut out, not the likely
moment — less 45 s of lead (`POWEROFF_SECONDS`), and acts only below 5%, on
readings that agree and with the charge already close to the cutoff, so one
odd gauge step or a wild trend can't trigger it; it re-checks the charger
first. The countdown, the menu (“Powers off at 10:30:11”, then when the
battery would cut out) and the notifications then count to the power-off.
The black box records the power-off, and deaths.json keeps that boot as a
power-off, not a death — otherwise its last reading, well before the cut,
would pull the cutoff up and the power-off earlier each time.

The profile popup centers its three buttons beneath the battery. A highlighted button
marks the active profile. Battery health and cycle count appear below the
buttons and refresh whenever the menu opens. Health is full capacity divided by design
capacity; unsupported readings show “unavailable”, and zero cycles is valid.
Below that, drainhound eta's time left — awake time on battery only — (to the
charge limit while charging) with its 80% interval (the battery is empty by t
exactly when the average draw over t reaches the remaining energy / t, so
P(T ≤ t) comes straight from the draw forecast), and how long the laptop would
last suspended from now: remaining energy over a log-normal fit of per-suspend
drain (longer suspends weigh more), with an 80% predictive interval for one new
suspend (spread between suspends plus the uncertainty of their mean). Both
lines refresh every second while the menu is open, so the last minutes' count
down to the second doesn't freeze.

Then two switch rows; a click that starts and ends on a row flips it and keeps
the menu open (Enter flips it and closes the menu). The release that ends a
press-drag from the bar does nothing, so opening the menu can't flip Comfort
mode and apply settings by accident:

- **Automatic experiments** (`drainhound experiment enable|disable`), with the
  runner's status under it: testing block N for how long this run has
  tested so far (minutes, then hours; suspends and pauses don't count), or
  paused and why (AC power, a 👎 rest, a setting unavailable or failing to
  change, nothing to test, or more testing would cost more than it saves:
  then the goal's combination is set, and switching goals switches it).
  Once the runner has priced testing, a second line gives the amount that
  pays best as an inequality: `30 h more: saves 50 Wh > costs 0.8 Wh`
  (saved over the next 30 days, spent now; `≤` when nothing pays).
  Turning them off restores a stock baseline; it no longer applies a
  recommendation (Comfort mode or the panel's Apply does that).
- **Comfort mode** picks the goal (`drainhound experiment goal comfort|battery
  --apply`): off is *Battery*. Under it, each goal's best measured combination
  — what it would change from your settings (the latest minute outside
  experiments; profile, ABM and boost, which follow AC, as on battery), the 👍 chance per block now → then for Comfort, and the battery
  life it adds or costs per full charge with its 90% interval. The chosen
  goal's line is bright. Switching applies that goal's combination unless
  experiments own the settings: then it only sets the goal, nothing is
  applied (the row says so), and turning experiments off restores stock
  rather than applying it. Risky settings (ASPM, APST, GPU clocks) are
  applied only with `--risky` on the command line and quarantined ones not
  at all, so a combination that includes them is applied without them.
  Applied settings last until reboot, on battery: on AC they go back to
  stock and return on battery. drainhound eta writes both lines to
  `$XDG_RUNTIME_DIR/drainhound/recommend.json` at each refit (every 15
  minutes); older than an hour, they show as waiting. The battery panel shows
  and applies the same file, so the menu and the panel show the same
  combinations.

Below that, battery use over the last 30 minutes on battery ranks apps and
hardware together in average watts and share: the five biggest, then
“Everything else (N)” for the rest with its three biggest named under it, so
the shares add up to 100% (rounded by largest remainder; before 2026-10-19 the
tail was dropped, ~5–20%). “Idle chip” lists what it is made of under it. The size of
the long-term power log is underneath (see
[Battery use and the power log](#battery-use-and-the-power-log)).
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
preserves the scroll position across refreshes, and closes as soon as the pointer
leaves sideways or upward (a 180 ms grace covers only the gap down into the panel,
or back up from it). Only one panel is open at a time: hovering another indicator
replaces it. It does not grab background input or re-enable the
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

`drainhound-collector.service` samples every two seconds and publishes an atomic cache
under `$XDG_RUNTIME_DIR/drainhound`. It stores no command lines; its only
persistent history is the power log below. CPU counters use PID plus process
start time, so PID reuse cannot inherit another process's CPU time. A long
sampling gap resets the window. Processes that finish entirely between samples
cannot be measured. The service has no filesystem sandbox: in a user manager
`ProtectSystem`/`ProtectHome` imply a private user namespace, which hides the
session's `/proc/PID/fd`, `fdinfo`, `io` and `exe`.

### Battery use, the panel and power experiments: drainhound

The power log, the battery menu's breakdown and time left, the battery panel
(`Mod+Shift+B`, `drainhound panel`), automatic power experiments, 👍/👎 votes,
background-app throttling and the cached-app freeze are
[drainhound](https://github.com/zyansheep/drainhound), its own project (it grew
here, in `cells/common/patches/system-monitor`, until 2026-10-20). Its
[docs](https://github.com/zyansheep/drainhound/tree/master/docs) cover the
models, the forecast, the experiments and their safety, and the data.

Here it is the `services.drainhound` NixOS module (flake input `drainhound`):
the settings shared by every niri host are in the
[Niri profile](../../../cells/nixos/profiles/graphics/niri.nix), this laptop's
(the helper's sudo rule, ABM's stock values, the `psr` boot arm) in
[power-conf.nix](../../../cells/nixos/hosts/isomorph/power-conf.nix). Its data
is in `~/.local/state/drainhound`, its live files (`snapshot.json`,
`eta.json`, `recommend.json`, `experiment.json`, which this bar's patched menu
and hover panels read) in `$XDG_RUNTIME_DIR/drainhound`. `drainhound check`
gathers every panel period from the real database, timed.

### Browser processes and readable labels

Process rows use application names and roles where the executable or installed
Electron entry point identifies them. Browser web-content processes are split
into individual PID rows (for example, `Floorp web · PID 12345`) instead of
combining every `Isolated Web Co` process into one large total. Helpers have
labels such as `Floorp · extensions` and `Floorp · media decoder`.

Floorp's packaged AutoConfig bridge calls `ChromeUtils.requestProcInfo()` every
five seconds after startup. It writes origins and up to three document titles
per process to `$XDG_RUNTIME_DIR/firefox-processes/<browser-pid>.json` (directory
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
