#include <cassert>
#include <cstdlib>
#include <iostream>

int main() {
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
  assert(estimate.title == "Time left: 1:15   80%: 1:00–1:30");
  assert(!waybar::powerMenuEta(eta, 1020).valid);  // stale
  std::ofstream(eta) << R"({"updated": 1000, "status": "Not charging"})";
  estimate = waybar::powerMenuEta(eta, 1001);
  assert(!estimate.valid && estimate.title == "Plugged in");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Charging", "hours": 0.5, "low": 0.4, "high": 0.7,
    "target": 90, "distribution": {"kind": "full", "t": [0.2, 0.5], "p": [1, 2, 3]}})";
  assert(!waybar::powerMenuEta(eta, 1001).valid);  // mismatched arrays
  assert(waybar::powerMenuClock(1.999) == "2:00" && waybar::powerMenuClock(-1) == "--");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Charging", "hours": 0.75, "low": 0.5, "high": 1.0,
    "target": 90, "ratio": 1.44, "ratio_low": 1.2, "ratio_high": 1.71,
    "banked": 1.7, "banked_low": 1.4, "banked_high": 2.05,
    "distribution": {"kind": "full", "t": [0.5, 1.0], "p": [1, 2]}})";
  assert(waybar::powerMenuEta(eta, 1001).title ==
         "On battery now: ~1:42   80%: 1:24–2:03   +1.4 min per minute\nTime to 90%: 0:45   80%: 0:30–1:00");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Not charging",
    "sleep": {"hours": 14.1, "low": 10.6, "high": 18.8, "watts": 1.03}})";
  estimate = waybar::powerMenuEta(eta, 1001);
  assert(!estimate.valid && estimate.sleep == "Asleep from now: ~14 h   80%: 11 h–19 h at 1.0 W");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 1, "low": 0.8, "high": 1.2,
    "sleep": {"hours": 6.5, "low": 4.95, "high": 8.25, "watts": 1.5},
    "distribution": {"kind": "empty", "t": [0.5, 1.5], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).sleep == "Asleep from now: ~6:30   80%: 4:57–8:15 at 1.5 W");
  setenv("TZ", "UTC", 1);
  tzset();
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 0.07, "low": 0.065, "high": 0.075,
    "cutout": 1250, "cutout_pm": 17.6, "distribution": {"kind": "empty", "t": [0.05, 0.1], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).title == "Cuts out at 00:20:50   in 4m 09s ±18 s");
  std::ofstream(eta) << R"({"updated": 1000, "status": "Discharging", "hours": 0.07, "low": 0.065, "high": 0.075,
    "cutout": 1250, "poweroff": 1220, "cutout_pm": 17.6,
    "distribution": {"kind": "empty", "t": [0.05, 0.1], "p": [1, 1]}})";
  assert(waybar::powerMenuEta(eta, 1001).title ==
         "Powers off at 00:20:20   in 3m 39s ±18 s\n(the battery cuts out at 00:20:50)");
  assert(waybar::powerMenuCountdown(41.4) == "41s" && waybar::powerMenuCountdown(-3) == "0s");

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
  std::cout << "Battery health/cycle, battery-use, time-left and experiment-control checks passed\n";
}
