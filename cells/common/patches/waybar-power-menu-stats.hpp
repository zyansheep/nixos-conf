#pragma once
#include <json/json.h>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

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

// Top apps by CPU share from the waybar-monitor snapshot, the usual proxy for
// battery use (Linux has no per-process power meter). Empty when stale.
inline std::vector<std::pair<std::string, std::string>> powerMenuProcesses(
    const std::filesystem::path& snapshot, double now, size_t limit = 5) {
  Json::Value state;
  std::ifstream input(snapshot);
  Json::CharReaderBuilder builder;
  std::string errors;
  std::vector<std::pair<std::string, std::string>> rows;
  if (!Json::parseFromStream(builder, input, &state, &errors) || !state.isObject() ||
      std::abs(now - state["timestamp"].asDouble()) >= 10 || !state["cpu"].isArray())
    return rows;
  for (const auto& entry : state["cpu"]) {
    if (rows.size() >= limit) break;
    if (entry.isObject() && entry["name"].isString() && entry["value"].isString())
      rows.emplace_back(entry["name"].asString(), entry["value"].asString());
  }
  return rows;
}
}  // namespace waybar
