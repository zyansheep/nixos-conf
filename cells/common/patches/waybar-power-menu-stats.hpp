#pragma once
#include <cmath>
#include <filesystem>
#include <fstream>
#include <string>

namespace waybar {
inline double powerMenuNumber(const std::filesystem::path& path) {
  std::ifstream input(path);
  double value = -1;
  return (input >> value) && std::isfinite(value) ? value : -1;
}

inline std::string powerMenuStats(const std::filesystem::path& root = "/sys/class/power_supply") {
  std::string result;
  std::error_code error;
  for (auto it = std::filesystem::directory_iterator(root, error);
       !error && it != std::filesystem::directory_iterator(); it.increment(error)) {
    const auto path = it->path();
    std::string type, scope;
    std::ifstream(path / "type") >> type;
    std::ifstream(path / "scope") >> scope;
    if (type != "Battery" || scope == "Device" || powerMenuNumber(path / "present") == 0) continue;
    double full = powerMenuNumber(path / "energy_full");
    double design = powerMenuNumber(path / "energy_full_design");
    if (full < 0 || design <= 0) {
      full = powerMenuNumber(path / "charge_full");
      design = powerMenuNumber(path / "charge_full_design");
    }
    double cycles = powerMenuNumber(path / "cycle_count");
    if (!result.empty()) result += "\n";
    result += path.filename().string() + " health: ";
    result += full >= 0 && design > 0
                  ? std::to_string(static_cast<int>(std::lround(100 * full / design))) + "%"
                  : "unavailable";
    result += "\nCycles: " + (cycles >= 0 ? std::to_string(static_cast<long long>(cycles)) : "unavailable");
  }
  return result.empty() ? "Battery health: unavailable\nCycles: unavailable" : result;
}
}  // namespace waybar
