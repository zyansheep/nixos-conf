#include <cassert>
#include <cstdlib>
#include <iostream>

int main() {
  setenv("TZ", "UTC", 1);  // Clock times in the menu's lines are local.
  tzset();
  char directory[] = "/tmp/waybar-power-test-XXXXXX";
  auto temporary = mkdtemp(directory);
  assert(temporary);
  const std::filesystem::path root(temporary);
  const auto battery = root / "BAT0";
  std::filesystem::create_directory(battery);
  auto put = [&](const char* name, const char* value) { std::ofstream(battery / name) << value; };
  put("type", "Battery");
  put("energy_full", "45000");
  put("energy_full_design", "50000");
  put("cycle_count", "0");
  assert(waybar::powerMenuStats(root) == "BAT0 health: 90%\nCycles: 0");
  put("energy_full_design", "0");
  put("charge_full", "4000");
  put("charge_full_design", "5000");
  put("cycle_count", "-1");
  assert(waybar::powerMenuStats(root) == "BAT0 health: 80%\nCycles: unavailable");
  put("charge_full_design", "invalid");
  assert(waybar::powerMenuStats(root) == "BAT0 health: unavailable\nCycles: unavailable");
  put("scope", "Device");
  assert(waybar::powerMenuStats(root) == "Battery health: unavailable\nCycles: unavailable");
  assert(waybar::powerMenuStats(root / "missing") == "Battery health: unavailable\nCycles: unavailable");

  const auto snapshot = root / "snapshot.json";
  std::ofstream(snapshot) << R"({"timestamp": 1000, "battery": {
    "title": "Battery use", "footer": "Power log: 3 samples", "rows": [
    {"name": "Floorp", "value": "4.0 W"}, 3, {"name": "Niri"},
    {"name": "Idle chip", "value": "8.2 W", "parts": [{"name": "Memory & I/O links", "value": "3.0 W"}, 4,
      {"name": "Cores & graphics"}]},
    {"name": "Display", "value": "0.5 W"}]}})";
  const auto use = waybar::powerMenuBattery(snapshot, 1005, 5);
  assert(use.title == "Battery use" && use.footer == "Power log: 3 samples");
  assert(use.rows.size() == 3 && use.rows[0].name == "Floorp" && use.rows[0].parts.empty() &&
         use.rows[2].value == "0.5 W");
  assert(use.rows[1].parts.size() == 1 && use.rows[1].parts[0].first == "Memory & I/O links" &&
         use.rows[1].parts[0].second == "3.0 W");
  assert(waybar::powerMenuBattery(snapshot, 1005, 1).rows.size() == 1);
  assert(waybar::powerMenuBattery(snapshot, 1010).title.empty());  // stale
  std::ofstream(snapshot) << R"({"timestamp": 1000, "cpu": []})";   // older collector
  assert(waybar::powerMenuBattery(snapshot, 1000).title.empty());
  std::ofstream(snapshot) << "not json";
  assert(waybar::powerMenuBattery(snapshot, 1000).rows.empty());
  assert(waybar::powerMenuBattery(root / "missing", 1000).title.empty());
  const auto eta = root / "eta.json";
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 1.25, "low": 1.0,
    "high": 1.5, "distribution": {"kind": "empty", "t": [0.5, 1.0, 1.5, 2.0], "p": [0, 1, 1, 0]}})";
  auto estimate = waybar::powerMenuEta(eta, 1005);
  assert(estimate.valid && estimate.kind == "empty" && estimate.hours.size() == 4);
  assert(estimate.title == "1:15 left · cuts out at 01:31");  // 1005 s is 00:16:45 UTC.
  assert(estimate.detail == "8 in 10 futures cut out between 01:16 and 01:46" && estimate.lines.empty());
  assert(!waybar::powerMenuEta(eta, 1020).valid);  // stale
  std::ofstream(eta) << R"({"updated": 1000, "status": "Not charging"})";
  estimate = waybar::powerMenuEta(eta, 1001);
  assert(!estimate.valid && estimate.title == "Plugged in");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Charging", "hours": 0.5, "low": 0.4, "high": 0.7,
    "target": 90, "distribution": {"kind": "full", "t": [0.2, 0.5], "p": [1, 2, 3]}})";
  estimate = waybar::powerMenuEta(eta, 1001);  // Mismatched arrays: no density, the lines still.
  assert(estimate.valid && estimate.density.empty() && estimate.kind == "full");
  assert(estimate.title == "Full (90%) at 00:46" &&
         estimate.detail == "in 0:30 · 8 in 10 futures between 00:40 and 00:58");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 1.25, "low": 1.0, "high": 1.5})";
  estimate = waybar::powerMenuEta(eta, 1005);  // No density (the gauge's estimate): the same lines.
  assert(estimate.valid && estimate.kind == "empty" && estimate.title == "1:15 left · cuts out at 01:31");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Charging", "hours": 0, "low": 0, "high": 0})";
  estimate = waybar::powerMenuEta(eta, 1001);  // At the target, with no limit set.
  assert(estimate.valid && estimate.title == "Full (100%)" && estimate.detail.empty());
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 1.0})";
  assert(!waybar::powerMenuEta(eta, 1001).valid);  // No interval: still estimating.
  assert(waybar::powerMenuClock(1.999) == "2:00" && waybar::powerMenuClock(-1) == "--");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Charging", "hours": 0.75, "low": 0.5, "high": 1.0,
    "target": 90, "ratio": 1.44, "ratio_low": 1.2, "ratio_high": 1.71,
    "banked": 1.7, "banked_low": 1.4, "banked_high": 2.05,
    "distribution": {"kind": "full", "t": [0.5, 1.0], "p": [1, 2]}})";
  estimate = waybar::powerMenuEta(eta, 1001);
  assert(estimate.title == "Full (90%) at 01:01" &&
         estimate.detail == "in 0:45 · 8 in 10 futures between 00:46 and 01:16");
  assert(estimate.lines.size() == 2 && estimate.lines[0] == "If unplugged now: 1:42 · cuts out 01:58 (01:40–02:19)" &&
         estimate.lines[1] == "Plugged in: +1.4 min of use per minute");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Not charging",
    "sleep": {"hours": 14.1, "low": 10.6, "high": 18.8, "watts": 1.03}})";
  estimate = waybar::powerMenuEta(eta, 1001);
  assert(!estimate.valid && estimate.sleep == "Asleep from now: ~14 h   80%: 11 h–19 h at 1.0 W");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 1, "low": 0.8, "high": 1.2,
    "sleep": {"hours": 6.5, "low": 4.95, "high": 8.25, "watts": 1.5},
    "distribution": {"kind": "empty", "t": [0.5, 1.5], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).sleep == "Asleep from now: ~6:30   80%: 4:57–8:15 at 1.5 W");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 0.07, "low": 0.065, "high": 0.075,
    "cutout": 1250, "cutout_pm": 17.6, "distribution": {"kind": "empty", "t": [0.05, 0.1], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).title == "Cuts out at 00:20:50   in 4m 09s ±18 s");
  assert(waybar::powerMenuEta(eta, 1001).detail.empty());
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 0.07, "low": 0.065, "high": 0.075,
    "cutout": 1250, "poweroff": 1220, "cutout_pm": 17.6,
    "distribution": {"kind": "empty", "t": [0.05, 0.1], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).title ==
         "Powers off at 00:20:20   in 3m 39s ±18 s\n(the battery cuts out at 00:20:50)");
  assert(waybar::powerMenuCountdown(41.4) == "41s" && waybar::powerMenuCountdown(-3) == "0s");

  const auto futures = root / "futures.json";
  std::ofstream(futures) << R"({"updated": 1000, "now": 990, "pct": 34.2, "cutoff_pct": 2.0, "target": 90.0,
    "empty": {"n": 3, "hours": 0.9, "low": 0.75, "high": 1.1, "runs": [
      {"end": 60, "pts": [[0, 34.2], [60, 2]]}, {"end": 40, "pts": [[0, 34.2], [40, 2]]},
      {"end": 50, "pts": [[0, 34.2]]}, 7, {"pts": [[0, 1], [1, 2]]}],
      "median": {"end": 54, "pts": [[0, 34.2], [54, 2]]}},
    "full": {"n": 1, "runs": [{"end": 80, "pts": [[0, 34.2], [80, 90]]}]}})";
  auto ahead = waybar::powerMenuFutures(futures, 1030);
  assert(ahead.valid && ahead.now == 990 && ahead.pct == 34.2 && ahead.cutoff == 2.0 && ahead.target == 90.0);
  assert(ahead.empty && ahead.empty->runs.size() == 2 && ahead.empty->runs[0].end == 40);  // By landing; bad runs out.
  assert(ahead.empty->median.end == 54 && ahead.empty->low == 0.75 && ahead.empty->runs[1].points.size() == 2);
  assert(!ahead.full);  // No median: nothing to draw.
  assert(!waybar::powerMenuFutures(futures, 1090).valid);  // Stale.
  std::ofstream(futures) << R"({"updated": 1000, "now": 1000, "pct": 80, "status": "Not charging"})";
  assert(!waybar::powerMenuFutures(futures, 1001).valid);  // Plugged in: no futures.
  assert(!waybar::powerMenuFutures(root / "missing", 1001).valid);
  assert(waybar::powerMenuWatts(" 8.8 W  63%") == 8.8 && waybar::powerMenuWatts("n/a") == 0.0);
  assert(waybar::powerMenuUseColor("Idle chip") == "#5f6d68" && waybar::powerMenuUseColor("USB devices") == "#8d9c94");
  assert(waybar::powerMenuUseColor("Floorp") == "#5fa8d3" && waybar::powerMenuUseColor("Kernel") == "#c784b0");
  assert(waybar::powerMenuUseColor("Everything else (62)") == "#9d8fd6" &&
         waybar::powerMenuUseColor("T3 Code") == "#d9663f");

  const auto state = root / "state", runtime = root / "runtime";
  std::filesystem::create_directories(state);
  std::filesystem::create_directories(runtime);
  auto modes = waybar::powerMenuModes(state, runtime, 1000);
  assert(!modes.experiments && !modes.comfort && modes.status.empty() && modes.best.empty());
  std::ofstream(state / "experiments-enabled") << "";
  std::ofstream(state / "experiments.json") << R"({"settings": ["aspm"], "goal": "comfort"})";
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "paused", "reason": "on AC power"})";
  std::ofstream(runtime / "recommend.json") << R"({"updated": 1000,
    "battery": {"change": "ASPM on", "gain": "+14 min", "interval": "−8 … +51", "delight": ""},
    "comfort": {"change": "full refresh rate", "gain": "−6 min", "interval": "−20 … +4", "delight": "2% → 9%"}})";
  modes = waybar::powerMenuModes(state, runtime, 1100);
  assert(modes.experiments && modes.comfort && modes.status == "Paused: on AC power");
  assert(modes.best.at("battery").change == "ASPM on" && modes.best.at("battery").interval == "−8 … +51");
  assert(modes.best.at("comfort").gain == "−6 min" && modes.best.at("comfort").delight == "2% → 9%");
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "running", "block": 12,
    "tested": 2672, "block_start": 1000, "block_end": 1240})";
  assert(waybar::powerMenuModes(state, runtime, 1100).status == "Testing block 12 for 46.20 minutes");
  assert(waybar::powerMenuModes(state, runtime, 1300).status == "Testing block 12 for 48.53 minutes");  // Block over.
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "running", "block": 21,
    "tested": 4800, "block_start": 1000, "block_end": 1240})";
  assert(waybar::powerMenuModes(state, runtime, 1060).status == "Testing block 21 for 1.35 hours");
  assert(waybar::powerMenuSpan(3599.9) == "1.00 hours" && waybar::powerMenuSpan(-5) == "0.00 minutes");
  assert(waybar::powerMenuSpan(65) == "1.08 minutes");
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "paused", "settled": true,
    "reason": "more testing would cost more than it saves"})";
  modes = waybar::powerMenuModes(state, runtime, 1100);
  assert(modes.settled && modes.status == "Paused: more testing would cost more than it saves");
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "running", "block": 3,
    "tested": 0, "block_start": 1000, "block_end": 1240, "worth": {"worth": true, "rows": [
      {"hours": 10, "saves": 11.6, "costs": 0.3}, {"hours": 30, "saves": 49.6, "costs": 0.84},
      {"hours": 100, "saves": 44.8, "costs": 2.6}]}})";
  assert(waybar::powerMenuModes(state, runtime, 1060).status ==
         "Testing block 3 for 1.00 minutes\n30 h more: saves 50 Wh > costs 0.8 Wh");
  std::ofstream(runtime / "experiment.json") << R"({"mode": "auto", "status": "paused", "settled": true,
    "reason": "more testing would cost more than it saves",
    "worth": {"worth": false, "rows": [{"hours": 10, "saves": -0.02, "costs": 0.78}]}})";
  assert(waybar::powerMenuModes(state, runtime, 1100).status ==
         "Paused: more testing would cost more than it saves\n10 h more: saves 0.0 Wh ≤ costs 0.8 Wh");
  assert(waybar::powerMenuWorth(Json::Value()).empty() && waybar::powerMenuWorth(Json::objectValue).empty());
  std::filesystem::remove(state / "experiments-enabled");
  assert(waybar::powerMenuModes(state, runtime, 1100).status.empty());
  assert(waybar::powerMenuModes(state, runtime, 1000 + 3601).best.empty());  // Stale.
  std::filesystem::remove_all(root);
  std::cout << "Battery health/cycle, battery-use, time-left, futures and experiment-control checks passed\n";
}
