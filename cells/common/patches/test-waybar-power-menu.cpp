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
  std::ofstream(snapshot) << R"({"timestamp": 1000, "cpu": [
    {"name": "Floorp", "value": "4.0%"}, 3, {"name": "Niri"},
    {"name": "Waybar", "value": "0.5%"}]})";
  const auto rows = waybar::powerMenuProcesses(snapshot, 1005, 5);
  assert(rows.size() == 2 && rows[0].first == "Floorp" && rows[1].second == "0.5%");
  assert(waybar::powerMenuProcesses(snapshot, 1005, 1).size() == 1);
  assert(waybar::powerMenuProcesses(snapshot, 1010).empty());  // stale
  std::ofstream(snapshot) << "not json";
  assert(waybar::powerMenuProcesses(snapshot, 1000).empty());
  assert(waybar::powerMenuProcesses(root / "missing", 1000).empty());
  std::filesystem::remove_all(root);
  std::cout << "Battery health/cycle and process checks passed\n";
}
