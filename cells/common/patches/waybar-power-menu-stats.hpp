#pragma once
#include <json/json.h>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <map>
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

// "4m 07s", or "41s" under a minute.
inline std::string powerMenuCountdown(double seconds) {
  const long total = std::max(0L, std::lround(seconds));
  if (total < 60) return std::to_string(total) + "s";
  return std::to_string(total / 60) + "m " + (total % 60 < 10 ? "0" : "") + std::to_string(total % 60) + "s";
}

// An epoch second as local "HH:MM:SS".
inline std::string powerMenuTimeOfDay(double epoch) {
  const std::time_t when = static_cast<std::time_t>(std::llround(epoch));
  std::tm local{};
  localtime_r(&when, &local);
  char text[16];
  std::strftime(text, sizeof text, "%H:%M:%S", &local);
  return text;
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
  std::string sleep;  // How long a suspend from now would last; empty until fitted.
};

// Hours as "14 h" from ten hours up, h:mm below.
inline std::string powerMenuLongHours(double hours) {
  return hours >= 10 ? std::to_string(std::lround(hours)) + " h" : powerMenuClock(hours);
}

inline PowerMenuEta powerMenuEta(const std::filesystem::path& path, double now) {
  Json::Value state;
  std::ifstream input(path);
  Json::CharReaderBuilder builder;
  std::string errors;
  PowerMenuEta eta;
  if (!Json::parseFromStream(builder, input, &state, &errors) || !state.isObject() ||
      std::abs(now - state["updated"].asDouble()) >= 15)
    return eta;
  const auto& asleep = state["sleep"];
  if (asleep.isObject() && asleep["hours"].isNumeric())
    eta.sleep = "Asleep from now: ~" + powerMenuLongHours(asleep["hours"].asDouble()) + "   80%: " +
                powerMenuLongHours(asleep["low"].asDouble()) + "–" +
                powerMenuLongHours(asleep["high"].asDouble()) + " at " +
                powerMenuTenths(asleep["watts"].asDouble()) + " W";
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
  // The last minutes: the second the embedded controller cuts the power, or
  // the clean power-off battery-eta does before it.
  if (eta.kind == "empty" && state["cutout"].isNumeric()) {
    const auto pm = " ±" + std::to_string(std::lround(state["cutout_pm"].asDouble())) + " s";
    const auto cutout = state["cutout"].asDouble();
    if (state["poweroff"].isNumeric())
      eta.title = "Powers off at " + powerMenuTimeOfDay(state["poweroff"].asDouble()) + "   in " +
                  powerMenuCountdown(state["poweroff"].asDouble() - now) + pm + "\n(the battery cuts out at " +
                  powerMenuTimeOfDay(cutout) + ")";
    else
      eta.title = "Cuts out at " + powerMenuTimeOfDay(cutout) + "   in " + powerMenuCountdown(cutout - now) + pm;
  }
  // While charging, lead with the time on battery banked so far (as the
  // outline does) and how much each minute plugged in adds.
  if (eta.kind == "full" && state["banked"].isNumeric()) {
    std::string line = "On battery now: ~" + powerMenuClock(state["banked"].asDouble()) + "   80%: " +
                       powerMenuClock(state["banked_low"].asDouble()) + "–" +
                       powerMenuClock(state["banked_high"].asDouble());
    if (state["ratio"].isNumeric())
      line += "   +" + powerMenuTenths(state["ratio"].asDouble()) + " min per minute";
    eta.title = line + "\n" + eta.title;
  }
  eta.valid = true;
  return eta;
}
inline Json::Value powerMenuJson(const std::filesystem::path& path) {
  Json::Value value;
  std::ifstream input(path);
  Json::CharReaderBuilder builder;
  std::string errors;
  return Json::parseFromStream(builder, input, &value, &errors) && value.isObject() ? value : Json::Value();
}

// The menu's experiment controls: automatic experiments (on while
// power-experiment's flag file exists) with the runner's status, the goal, and
// what each goal's best measured combination would change (battery-eta's
// recommend.json, refitted every 15 minutes; missing when stale).
struct PowerMenuGoal {
  std::string change, gain, interval, delight;  // "ASPM on", "+14 min", "−8 … +51", "2% → 9%"
};

struct PowerMenuModes {
  bool experiments = false;
  std::string status;  // "Testing block 12", "Paused: on AC power", or empty while off.
  bool comfort = false;  // The goal: comfort, or battery.
  std::map<std::string, PowerMenuGoal> best;  // By goal.
};

inline PowerMenuModes powerMenuModes(const std::filesystem::path& state, const std::filesystem::path& runtime,
                                     double now) {
  PowerMenuModes modes;
  std::error_code error;
  modes.experiments = std::filesystem::exists(state / "experiments-enabled", error);
  modes.comfort = powerMenuJson(state / "experiments.json")["goal"].asString() == "comfort";
  const auto run = powerMenuJson(runtime / "experiment.json");
  const bool automatic = run["mode"].asString() == "auto";
  if (modes.experiments) {
    if (automatic && run["status"].asString() == "running")
      modes.status = "Testing block " + std::to_string(run["block"].asInt());
    else if (automatic && run["status"].asString() == "paused")
      modes.status = "Paused: " + run["reason"].asString();
    else
      modes.status = "Starting…";
  }
  const auto recommend = powerMenuJson(runtime / "recommend.json");
  if (recommend.isObject() && now - recommend["updated"].asDouble() < 3600)
    for (const char* goal : {"battery", "comfort"}) {
      const auto& entry = recommend[goal];
      if (entry.isObject() && !entry["change"].asString().empty())
        modes.best[goal] = {entry["change"].asString(), entry["gain"].asString(), entry["interval"].asString(),
                            entry["delight"].asString()};
    }
  return modes;
}
}  // namespace waybar
