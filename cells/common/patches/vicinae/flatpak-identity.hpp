#pragma once

#include <cerrno>
#include <fcntl.h>
#include <optional>
#include <string>
#include <string_view>
#include <unistd.h>

namespace AppIdentity {
// Empty means native; nullopt means identity could not be established.
inline std::optional<std::string> parseFlatpakInfo(std::string_view text) {
  bool application = false;
  while (!text.empty()) {
    auto end = text.find('\n');
    auto line = text.substr(0, end);
    if (end == std::string_view::npos) text = {};
    else text.remove_prefix(end + 1);
    auto first = line.find_first_not_of(" \t\r");
    if (first == std::string_view::npos) continue;
    line.remove_prefix(first);
    line = line.substr(0, line.find_last_not_of(" \t\r") + 1);
    if (line.front() == '[') application = line == "[Application]";
    if (application && line.starts_with("name=")) {
      auto name = line.substr(5);
      if (!name.empty()) return std::string(name);
    }
  }
  return std::nullopt;
}

inline std::optional<std::string> process(int pid, const std::string &proc = "/proc") {
  if (pid <= 0) return std::nullopt;
  // Opening root first distinguishes an absent marker from an inaccessible or
  // exited process, and pins the directory used for the subsequent lookup.
  int root = open((proc + "/" + std::to_string(pid) + "/root").c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (root < 0) return std::nullopt;
  int marker = openat(root, ".flatpak-info", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  int error = errno;
  close(root);
  if (marker < 0) return error == ENOENT ? std::optional<std::string>("") : std::nullopt;
  std::string text;
  char buffer[4096];
  ssize_t count;
  while ((count = read(marker, buffer, sizeof(buffer))) > 0) {
    text.append(buffer, count);
    if (text.size() > 65536) { close(marker); return std::nullopt; }
  }
  close(marker);
  if (count < 0) return std::nullopt;
  return parseFlatpakInfo(text);
}

inline bool matches(std::string_view expected, const std::optional<std::string> &actual) {
  return actual && expected == *actual;
}
} // namespace AppIdentity
