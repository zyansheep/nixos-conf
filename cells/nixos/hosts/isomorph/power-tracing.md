# Continuous power history on isomorph

Investigated 2026-09-15. Recommendation: a small local collector for measured
battery drain, with optional Scaphandre estimates for CPU energy by application.
No new monitoring daemon is enabled by the menu changes.

## What this machine exposes

Inspected the running Ryzen 5 7640U Framework laptop:

| Source | Observed availability | Meaning / limitation |
| --- | --- | --- |
| `/sys/class/power_supply/BAT1` | Charge, current, voltage, status, cycles | Whole-laptop battery drain while discharging. No `power_now` or `energy_now`; compute watts as `current_now * voltage_now / 1e12`. |
| `/sys/class/powercap/intel-rapl:0` | `package-0`, cumulative `energy_uj`; read denied to ordinary user | CPU package energy, including whatever this platform's domain covers. Name retains `intel-rapl` even on AMD. A restricted privileged collector is needed. |
| `/sys/class/powercap/intel-rapl:0:0` | `core`, cumulative energy; read denied | Nested domain: do not add this to package energy. |
| `amdgpu` hwmon | `power1_average`, `power1_input`, label `PPT` | Readable telemetry. Domain boundaries need validation before treating this as GPU-only or adding it to package energy. Discover by sensor name, not unstable `hwmon1` index. |
| UPower D-Bus service | Name not available/activatable in this session | No existing UPower history to reuse here. Could enable it, but direct battery sysfs readings already work. |

The RAPL counter interfaces and rollover range are documented by the
[Linux powercap framework](https://www.kernel.org/doc/html/latest/power/powercap/powercap.html).
A two-second read-only `power-lab turbostat 2` sample also returned valid
`PkgWatt` and `CorWatt` values (14.02 W and 6.12 W during this build session).
This validates that privileged package/core telemetry works, not its absolute
accuracy or idle consumption. Direct sysfs counter-delta validation remains.

Battery readings cover display, Wi-Fi, storage, CPU and the rest of the laptop.
On AC, battery charging power is **not** computer power consumption. Mark the
whole-laptop trace unavailable on AC; CPU-package monitoring can continue.

## Existing tools

| Tool | What it offers | Fit for this popup |
| --- | --- | --- |
| [Scaphandre](https://github.com/hubblo-org/scaphandre) | Background energy metrology, RAPL counters and process estimates, exporters | Best candidate for optional all-process CPU attribution. Adapt/export into local history rather than requiring a dashboard stack. |
| [PowerJoular](https://github.com/joular/powerjoular) | RAPL Intel/AMD support, process or application selection, CSV output, systemd daemon | Good for focused application experiments. AMD integrated-GPU attribution is not established by its documented Nvidia GPU support. |
| [PowerTOP](https://github.com/fenrus75/powertop) | Power diagnostics, wakeups, device activity, tuning and reports | Already installed, with `power-lab powertop` report support. Useful for explaining idle drain; less suitable as a persistent structured history backend. Existing auto-tune configuration does not record history. |
| [UPower](https://upower.freedesktop.org/docs/Device.html) | Battery `EnergyRate`, `GetHistory`, state and capacity via D-Bus | Useful standard interface for battery history when enabled; no process attribution. Check `HasHistory` rather than assuming every device supports it. |

Scaphandre assigns energy using process CPU-time shares. That is an estimate,
not a wattmeter attached to each application. It cannot precisely attribute a
browser's display, networking and GPU costs. Multiprocess applications require
aggregation; its documentation describes this distinction and aggregation
through exporters. [Scaphandre's attribution explanation](https://github.com/hubblo-org/scaphandre/blob/main/docs_src/explanations/how-scaph-computes-per-process-power-consumption.md).

## Proposed collector

Start with a five-second sampling interval, then measure the collector's own
CPU time, wakeups and disk writes. A service should run independently of the
menu, which only reads a cached result.

1. Read battery current, voltage and state. Read RAPL energy counters through a
   narrowly scoped service if CPU attribution is enabled; keep the GUI unprivileged.
2. Store timestamps, boot ID, AC/discharge state and sensor validity. Preserve
   missing periods; never integrate a stale watt reading across suspend, reboot,
   charger transitions or a long sampling gap.
3. Integrate battery watts using adjacent valid samples:
   `Wh += (previous_W + current_W) / 2 * elapsed_seconds / 3600`.
   RAPL energy deltas already represent area under the curve: divide microjoules
   by `3.6e9` to get Wh, handling rollover using `max_energy_range_uj`. Do not
   integrate the same domain twice.
4. Aggregate process estimates by application/systemd scope when possible;
   identify processes with PID plus start time to handle PID reuse. Retain an
   explicit unattributed category. Short-lived processes between samples are
   a coverage limitation, not zero-energy work.
5. Use a bounded SQLite history: five-second samples for a day, minute aggregates
   for a month, batched writes. Store application identity, not full command
   lines. Keep measured battery totals separate from estimated CPU breakdowns.

This is a proposed design, not a measured overhead claim. Before enabling it by
default, compare idle draw with and without collection, validate RAPL against
controlled CPU load, and check battery discharge over a longer interval.

## Battery-window integration

The compact GTK3 menu can contain a non-interactive `GtkDrawingArea` below the
profile buttons and health/cycles footer. Cairo can draw a small filled watts
history, with `Wh over last hour`, average watts, and sample coverage beside it.
GTK4 is not required for this. Read a cached snapshot at menu-open time so
history does not introduce startup latency.

For example, 10 W sustained for 30 minutes consumes 5 Wh: the filled graph's
area represents consumed energy. A top-app list should say **estimated CPU Wh**,
while the headline says **measured battery Wh**. Do not stack process CPU
estimates into a chart claiming to account exactly for the whole battery.

A `Power history…` action could open a larger non-modal view with 15-minute,
hour and day ranges and app grouping. Keep the native selector's quick
press-drag-release gesture; the detail view can allow background interaction.

Suggested implementation order: battery-only history first; validate privileged
RAPL readings second; trial Scaphandre attribution third. This yields a useful
whole-laptop trace even if application attribution proves too noisy.
