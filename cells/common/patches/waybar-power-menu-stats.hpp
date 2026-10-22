#pragma once
#include <json/json.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
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

struct PowerMenuUse {
  std::string name, value;
  std::vector<std::pair<std::string, std::string>> parts;  // What the row is made of, if split (the idle chip).
};

struct PowerMenuBattery {
  std::string title;
  std::vector<PowerMenuUse> rows;
  std::string footer;
};

// Battery use by app and hardware, attributed by drainhound from measured
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
      if (!entry.isObject() || !entry["name"].isString() || !entry["value"].isString()) continue;
      PowerMenuUse row{entry["name"].asString(), entry["value"].asString(), {}};
      for (const auto& part : entry["parts"])
        if (part.isObject() && part["name"].isString() && part["value"].isString())
          row.parts.emplace_back(part["name"].asString(), part["value"].asString());
      result.rows.push_back(std::move(row));
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

// A duration as "46.20 minutes" under an hour, "1.35 hours" from one up.
inline std::string powerMenuSpan(double seconds) {
  long hundredths = std::lround(std::max(0.0, seconds) / 0.6);  // Of a minute.
  std::string unit = " minutes";
  if (hundredths >= 6000) {
    hundredths = std::lround(seconds / 36);  // Of an hour.
    unit = " hours";
  }
  return std::to_string(hundredths / 100) + (hundredths % 100 < 10 ? ".0" : ".") + std::to_string(hundredths % 100) +
         unit;
}

inline std::string powerMenuTenths(double value) {
  const long tenths = std::lround(value * 10);
  return std::to_string(tenths / 10) + "." + std::to_string(std::labs(tenths % 10));
}

// Whether more automatic testing pays (report.testing_value: drainhound experiment's
// `worth`), as an inequality for the amount of testing that pays best:
// "30 h more: saves 50 Wh > costs 0.8 Wh" (saved over the next 30 days, spent
// now; ≤ when nothing pays). Empty until priced.
inline std::string powerMenuWorth(const Json::Value& worth) {
  const Json::Value* best = nullptr;
  auto net = [](const Json::Value& row) { return row["saves"].asDouble() - row["costs"].asDouble(); };
  for (const auto& row : worth["rows"])
    if (row.isObject() && (best == nullptr || net(row) > net(*best))) best = &row;
  if (best == nullptr) return "";
  auto wh = [](double value) {
    value = std::max(0.0, value);
    return (value >= 9.5 ? std::to_string(std::lround(value)) : powerMenuTenths(value)) + " Wh";
  };
  const auto& row = *best;
  return std::to_string(std::lround(row["hours"].asDouble())) + " h more: saves " + wh(row["saves"].asDouble()) +
         (net(row) > 0 ? " > " : " ≤ ") + "costs " + wh(row["costs"].asDouble());
}

// drainhound eta's estimate: title line, a faded line under it, more lines
// (while charging: unplugged now, and what a minute plugged in buys), and the
// time-left (or time-to-limit) density on a grid of hours. `valid` is false
// when stale or not estimating.
struct PowerMenuEta {
  bool valid = false;
  std::string title, detail;
  std::vector<std::string> lines;
  std::string kind;  // "empty" or "full"
  std::vector<double> hours, density;
  double median = 0, low = 0, high = 0;
  std::string sleep;  // How long a suspend from now would last; empty until fitted.
};

// An epoch second as local "HH:MM".
inline std::string powerMenuHourMinute(double epoch) { return powerMenuTimeOfDay(epoch).substr(0, 5); }

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
  const auto status = state["status"].asString();
  const auto at = [&](double hours) { return powerMenuHourMinute(now + hours * 3600); };
  // Plugged in or charging: what unplugging now would leave (the time on battery
  // banked so far, as the outline shows it).
  const auto unplugged = [&] {
    if (state["banked"].isNumeric())
      eta.lines.push_back("If unplugged now: " + powerMenuClock(state["banked"].asDouble()) + " · cuts out " +
                          at(state["banked"].asDouble()) + " (" + at(state["banked_low"].asDouble()) + "–" +
                          at(state["banked_high"].asDouble()) + ")");
  };
  if (!state["hours"].isNumeric() || !state["low"].isNumeric() || !state["high"].isNumeric()) {
    eta.title = status == "Discharging" || status == "Charging" ? "Estimating time left…" : "Plugged in";
    if (status != "Discharging") unplugged();
    return eta;
  }
  // The density, when eta.json has one; the lines need only the median and the 80% ends.
  const auto& distribution = state["distribution"];
  if (distribution.isObject() && distribution["t"].isArray() && distribution["p"].isArray() &&
      distribution["t"].size() == distribution["p"].size() && distribution["t"].size() >= 2)
    for (Json::ArrayIndex i = 0; i < distribution["t"].size(); i++) {
      eta.hours.push_back(distribution["t"][i].asDouble());
      eta.density.push_back(distribution["p"][i].asDouble());
    }
  eta.kind = distribution.isObject() && distribution["kind"].isString() ? distribution["kind"].asString()
             : status == "Charging"                                     ? "full"
                                                                        : "empty";
  eta.median = state["hours"].asDouble();
  eta.low = state["low"].asDouble();
  eta.high = state["high"].asDouble();
  // As the panel's answers: when it is full (or how long it lasts and when it
  // cuts out), and the 80% interval as clock times, the eight futures in ten
  // between them that futures.json draws.
  if (eta.kind == "full") {
    const auto target = state["target"].isNumeric() ? std::lround(state["target"].asDouble()) : 100;
    eta.title = "Full (" + std::to_string(target) + "%)";
    if (eta.median > 0) {
      eta.title += " at " + at(eta.median);
      eta.detail = "in " + powerMenuClock(eta.median) + " · 8 in 10 futures between " + at(eta.low) + " and " +
                   at(eta.high);
    }
  } else {
    eta.title = powerMenuClock(eta.median) + " left · cuts out at " + at(eta.median);
    eta.detail = "8 in 10 futures cut out between " + at(eta.low) + " and " + at(eta.high);
  }
  // The last minutes: the second the embedded controller cuts the power, or
  // the clean power-off drainhound eta does before it.
  if (eta.kind == "empty" && state["cutout"].isNumeric()) {
    const auto pm = " ±" + std::to_string(std::lround(state["cutout_pm"].asDouble())) + " s";
    const auto cutout = state["cutout"].asDouble();
    eta.detail.clear();
    if (state["poweroff"].isNumeric())
      eta.title = "Powers off at " + powerMenuTimeOfDay(state["poweroff"].asDouble()) + "   in " +
                  powerMenuCountdown(state["poweroff"].asDouble() - now) + pm + "\n(the battery cuts out at " +
                  powerMenuTimeOfDay(cutout) + ")";
    else
      eta.title = "Cuts out at " + powerMenuTimeOfDay(cutout) + "   in " + powerMenuCountdown(cutout - now) + pm;
  }
  // While charging: what unplugging now would leave, and what a minute plugged in buys.
  if (eta.kind == "full") unplugged();
  if (eta.kind == "full" && state["ratio"].isNumeric())
    eta.lines.push_back("Plugged in: +" + powerMenuTenths(state["ratio"].asDouble()) + " min of use per minute");
  eta.valid = true;
  return eta;
}

// drainhound eta's sampled futures (futures.json): runs of its own models from
// `now` to where each lands (the cutoff, or while charging the charge target),
// run k landing at the forecast's (k + ½)/n quantile, so the dots where they
// land are the distribution. `valid` is false when missing or stale.
struct PowerMenuRun {
  double end = 0;                                  // Minutes from `now` to where it lands.
  std::vector<std::pair<double, double>> points;  // (minutes, charge %).
};

struct PowerMenuScenario {
  std::vector<PowerMenuRun> runs;  // By when they land.
  PowerMenuRun median;
  double hours = 0, low = 0, high = 0;  // The forecast's median and 80% ends.
};

struct PowerMenuFutures {
  bool valid = false;
  bool onBattery = true;  // Else (plugged in or charging) `empty` is what unplugging now would do.
  double now = 0, pct = 0, cutoff = 0, target = 100;
  std::optional<PowerMenuScenario> empty, full;  // To the cutoff; to the target (charging).
};

inline bool powerMenuRun(const Json::Value& value, PowerMenuRun& run) {
  if (!value.isObject() || !value["end"].isNumeric() || !value["pts"].isArray()) return false;
  run.end = value["end"].asDouble();
  for (const auto& point : value["pts"])
    if (point.isArray() && point.size() == 2 && point[0].isNumeric() && point[1].isNumeric())
      run.points.emplace_back(point[0].asDouble(), point[1].asDouble());
  return std::isfinite(run.end) && run.points.size() >= 2;
}

inline std::optional<PowerMenuScenario> powerMenuScenario(const Json::Value& value) {
  if (!value.isObject() || !value["runs"].isArray()) return std::nullopt;
  PowerMenuScenario scenario;
  for (const auto& entry : value["runs"])
    if (PowerMenuRun run; powerMenuRun(entry, run)) scenario.runs.push_back(std::move(run));
  if (scenario.runs.empty() || !powerMenuRun(value["median"], scenario.median)) return std::nullopt;
  std::sort(scenario.runs.begin(), scenario.runs.end(), [](const auto& a, const auto& b) { return a.end < b.end; });
  scenario.hours = value["hours"].asDouble();
  scenario.low = value["low"].asDouble();
  scenario.high = value["high"].asDouble();
  return scenario;
}

inline PowerMenuFutures powerMenuFutures(const std::filesystem::path& path, double now) {
  Json::Value state;
  std::ifstream input(path);
  Json::CharReaderBuilder builder;
  std::string errors;
  PowerMenuFutures futures;
  // Rewritten at least every 30 s while there are futures.
  if (!Json::parseFromStream(builder, input, &state, &errors) || !state.isObject() ||
      std::abs(now - state["updated"].asDouble()) >= 90 || !state["now"].isNumeric() || !state["pct"].isNumeric())
    return futures;
  futures.now = state["now"].asDouble();
  futures.pct = state["pct"].asDouble();
  const auto status = state["status"].asString();
  futures.onBattery = status.empty() || status == "Discharging";
  futures.cutoff = state["cutoff_pct"].isNumeric() ? state["cutoff_pct"].asDouble() : 0.0;
  futures.target = state["target"].isNumeric() ? state["target"].asDouble() : 100.0;
  futures.empty = powerMenuScenario(state["empty"]);
  futures.full = powerMenuScenario(state["full"]);
  futures.valid = futures.empty.has_value() || futures.full.has_value();
  return futures;
}

// A battery-use row's watts: the number its value starts with (" 8.8 W  63%").
inline double powerMenuWatts(const std::string& value) {
  char* end = nullptr;
  const double watts = std::strtod(value.c_str(), &end);
  return end != value.c_str() && std::isfinite(watts) && watts > 0 ? watts : 0.0;
}

// A battery-use row's colour in the menu's bar, as the battery panel's stack
// colours its layers: the idle chip, the display and the other devices, then an
// app by its group (drainhound's default groups, by name prefix), else "other".
inline std::string powerMenuUseColor(const std::string& name) {
  if (name == "Idle chip") return "#5f6d68";
  if (name == "Display") return "#d6a84a";
  for (const char* device : {"Rest of system", "Wi-Fi", "Storage", "USB devices", "Audio", "Keyboard backlight"})
    if (name == device) return "#8d9c94";
  std::string lower = name;
  std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return std::tolower(c); });
  const std::pair<const char*, std::vector<const char*>> groups[] = {
      {"#5fa8d3", {"firefox", "floorp", "librewolf", "zen", "waterfox", "chromium", "chrome", "brave", "vivaldi"}},
      {"#d9663f", {"code", "vscodium", "cursor", "zed", "t3 code", "emacs", "nvim", "neovim", "vim", "helix", "foot",
                   "kitty", "alacritty", "wezterm", "ghostty", "rustc", "cargo", "gcc", "clang", "node", "npm",
                   "bun", "nix", "claude", "codex", "opencode", "python"}},
      {"#6cbf8b", {"vesktop", "discord", "signal", "slack", "telegram", "element", "spotify", "mpv", "vlc",
                   "pipewire", "wireplumber"}},
      {"#c784b0", {"kernel", "niri", "sway", "hyprland", "waybar", "xwayland", "systemd", "dbus", "drainhound",
                   "swaync", "fcitx"}}};
  for (const auto& [color, prefixes] : groups)
    for (const char* prefix : prefixes)
      if (lower.rfind(prefix, 0) == 0) return color;
  return "#9d8fd6";  // "Everything else", and apps no group claims.
}

inline Json::Value powerMenuJson(const std::filesystem::path& path) {
  Json::Value value;
  std::ifstream input(path);
  Json::CharReaderBuilder builder;
  std::string errors;
  return Json::parseFromStream(builder, input, &value, &errors) && value.isObject() ? value : Json::Value();
}

// The menu's experiment controls: automatic experiments (on while
// drainhound experiment's flag file exists) with the runner's status, the goal, and
// what each goal's best measured combination would change (drainhound eta's
// recommend.json, refitted every 15 minutes; missing when stale).
struct PowerMenuGoal {
  std::string change, gain, interval, delight;  // "ASPM on", "+14 min", "−8 … +51", "2% → 9%"
};

struct PowerMenuModes {
  bool experiments = false;
  // "Testing block 12 for 46.20 minutes", "Paused: on AC power", or empty while off; then, once
  // priced, a line with whether more testing pays (powerMenuWorth).
  std::string status;
  bool settled = false;  // Paused because more testing isn't worth it: the goal's combination is set.
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
    if (automatic && run["status"].asString() == "running") {
      // Time tested this run: the blocks before this one (awake), and this one so far.
      const double start = run["block_start"].asDouble(), length = run["block_end"].asDouble() - start;
      const double tested = run["tested"].asDouble() + std::clamp(now - start, 0.0, std::max(0.0, length));
      modes.status = "Testing block " + std::to_string(run["block"].asInt()) + " for " + powerMenuSpan(tested);
    } else if (automatic && run["status"].asString() == "paused") {
      modes.status = "Paused: " + run["reason"].asString();
      modes.settled = run["settled"].asBool();
    } else {
      modes.status = "Starting…";
    }
    if (automatic) {
      const auto worth = powerMenuWorth(run["worth"]);
      if (!worth.empty()) modes.status += "\n" + worth;
    }
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
