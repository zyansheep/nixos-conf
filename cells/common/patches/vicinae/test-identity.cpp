#include "flatpak-identity.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>

int main() {
  using namespace AppIdentity;
  assert(parseFlatpakInfo("[Application]\nname=dev.vencord.Vesktop\n[Instance]\nname=wrong\n") == "dev.vencord.Vesktop");
  assert(!parseFlatpakInfo("[Instance]\nname=wrong\n"));
  assert(!parseFlatpakInfo("[Application]\nname=\n"));
  assert(matches("", std::string("")));
  assert(matches("app.a", std::string("app.a")));
  assert(!matches("app.a", std::string("app.b")));
  assert(!matches("", std::string("app.a")));
  assert(!matches("app.a", std::string("")));
  assert(!matches("", std::nullopt));
  assert(!matches("app.a", std::nullopt));
  char temp[] = "/tmp/vicinae-identity-XXXXXX";
  auto dir = mkdtemp(temp); assert(dir);
  std::filesystem::path base(dir);
  auto root = base / "42/root";
  std::filesystem::create_directories(root);
  assert(process(42, dir) == "");
  assert(!process(43, dir));
  assert(!process(0, dir));
  { std::ofstream f(root / ".flatpak-info"); f << "[Application]\nname=app.a\n"; }
  assert(process(42, dir) == "app.a");
  { std::ofstream f(root / ".flatpak-info"); f << "malformed"; }
  assert(!process(42, dir));
  std::filesystem::remove(root / ".flatpak-info");
  std::filesystem::create_symlink("missing", root / ".flatpak-info");
  assert(!process(42, dir));
  std::filesystem::remove_all(base);
}
