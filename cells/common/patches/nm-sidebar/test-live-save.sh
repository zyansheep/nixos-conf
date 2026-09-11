#!/usr/bin/env bash
set -euo pipefail
test_sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$test_sources" rev-parse --show-toplevel)
cd "$repo_root"

nix develop --no-update-lock-file .#nm-sidebar --command bash -c '
  set -euo pipefail
  scratch=$(mktemp -d /tmp/nm-sidebar-save-test.XXXXXX)
  trap '\''rm -rf -- "$scratch"'\'' EXIT
  cd "$scratch"
  source "$stdenv/setup"
  unpackPhase
  cd "$sourceRoot"
  patchPhase
  $CC -Wno-deprecated-declarations -Isrc "$1/test-profile-save.c" \
    src/actions/*.c src/data/*.c src/sections/*.c \
    $(pkg-config --cflags --libs libnm libadwaita-1 gtk4 gio-2.0) \
    -o "$scratch/test-save"
  "$scratch/test-save"
  "$scratch/test-save" --enterprise
  if [[ ${2:-} == --ui ]]; then
    sections=(src/sections/*.c)
    sources=()
    for file in "${sections[@]}"; do
      [[ $file == src/sections/connection-settings.c ]] || sources+=("$file")
    done
    $CC -Wno-deprecated-declarations -Isrc "$1/test-profile-page.c" \
      src/actions/*.c src/data/*.c "${sources[@]}" \
      $(pkg-config --cflags --libs libnm libadwaita-1 gtk4 gio-2.0) \
      -o "$scratch/test-page"
    "$scratch/test-page"
    "$scratch/test-page" --inherited-mac
  fi
' bash "$test_sources" "${1:-}"
