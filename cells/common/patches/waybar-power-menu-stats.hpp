#pragma once
#include <json/json.h>
#include <algorithm>
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

struct PowerMenuBattery {
  std::string title;
  std::vector<std::pair<std::string, std::string>> rows;
  std::string footer;
};

// Battery use by app and hardware, attributed by waybar-monitor from measured
// battery and chip power. Empty title when the snapshot is missing or stale.
inline PowerMenuBattery powerMenuBattery(const std::filesystem::path& snapshot, double now,
                                         size_t limit = 6) {
  Json::Value state;
  std::ifstream input(snapshot);
  Json::CharReaderBuilder builder;
  std::string errors;
  PowerMenuBattery result;
  if (!Json::parseFromStream(builder, input, &state, &errors) || !state.isObject() ||
      std::abs(now - state["timestamp"].asDouble()) >= 10 || !state["battery"].isObject())
    return result;
  const auto& battery = state["battery"];
  if (battery["title"].isString()) result.title = battery["title"].asString();
  if (battery["footer"].isString()) result.footer = battery["footer"].asString();
  if (battery["rows"].isArray()) {
    for (const auto& entry : battery["rows"]) {
      if (result.rows.size() >= limit) break;
      if (entry.isObject() && entry["name"].isString() && entry["value"].isString())
        result.rows.emplace_back(entry["name"].asString(), entry["value"].asString());
    }
  }
  return result;
}
inline std::string powerMenuClock(double hours) {
  if (!std::isfinite(hours) || hours < 0 || hours > 99) return "--";
  const long minutes = std::lround(hours * 60);
  return std::to_string(minutes / 60) + (minutes % 60 < 10 ? ":0" : ":") + std::to_string(minutes % 60);
}

inline std::string powerMenuTenths(double value) {
  const long tenths = std::lround(value * 10);
  return std::to_string(tenths / 10) + "." + std::to_string(std::labs(tenths % 10));
}

// battery-eta's estimate: title line plus the time-left (or time-to-limit)
// density on a grid of hours. `valid` is false when stale or not estimating.
struct PowerMenuEta {
  bool valid = false;
  std::string title;
  std::string kind;  // "empty" or "full"
  std::vector<double> hours, density;
  double median = 0, low = 0, high = 0;
};

inline PowerMenuEta powerMenuEta(const std::filesystem::path& path, double now) {
  Json::Value state;
  std::ifstream input(path);
  Json::CharReaderBuilder builder;
  std::string errors;
  PowerMenuEta eta;
  if (!Json::parseFromStream(builder, input, &state, &errors) || !state.isObject() ||
      std::abs(now - state["updated"].asDouble()) >= 15)
    return eta;
  const auto& distribution = state["distribution"];
  if (!state["hours"].isNumeric() || !distribution.isObject() || !distribution["t"].isArray() ||
      distribution["t"].size() != distribution["p"].size() || distribution["t"].size() < 2) {
    eta.title = state["status"].asString() == "Discharging" || state["status"].asString() == "Charging"
                    ? "Estimating time left…"
                    : "Plugged in";
    return eta;
  }
  for (Json::ArrayIndex i = 0; i < distribution["t"].size(); i++) {
    eta.hours.push_back(distribution["t"][i].asDouble());
    eta.density.push_back(distribution["p"][i].asDouble());
  }
  eta.kind = distribution["kind"].asString();
  eta.median = state["hours"].asDouble();
  eta.low = state["low"].asDouble();
  eta.high = state["high"].asDouble();
  eta.title = (eta.kind == "full" ? "Time to " + std::to_string(state["target"].asInt()) + "%: "
                                  : std::string("Time left: ")) +
              powerMenuClock(eta.median) + "   80%: " + powerMenuClock(eta.low) + "–" +
              powerMenuClock(eta.high);
  // While charging, lead with minutes of battery use bought per minute plugged in.
  if (eta.kind == "full" && state["ratio"].isNumeric())
    eta.title = "Charging: ×" + powerMenuTenths(state["ratio"].asDouble()) + " use per minute (80%: ×" +
                powerMenuTenths(state["ratio_low"].asDouble()) + "–" +
                powerMenuTenths(state["ratio_high"].asDouble()) + ")\n" + eta.title;
  eta.valid = true;
  return eta;
}
}  // namespace waybar
