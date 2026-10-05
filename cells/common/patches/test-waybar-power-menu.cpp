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
    {"name": "Display", "value": "0.5 W"}]}})";
  const auto use = waybar::powerMenuBattery(snapshot, 1005, 5);
  assert(use.title == "Battery use" && use.footer == "Power log: 3 samples");
  assert(use.rows.size() == 2 && use.rows[0].first == "Floorp" &&
         use.rows[1].second == "0.5 W");
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
  std::filesystem::remove_all(root);
  std::cout << "Battery health/cycle, battery-use and time-left checks passed\n";
}
