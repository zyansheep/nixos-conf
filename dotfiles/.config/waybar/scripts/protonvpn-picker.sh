#!/usr/bin/env bash
# Server picker for the waybar custom/protonvpn module (on-click).
#
# Picks at COUNTRY granularity, not individual servers. `protonvpn servers`
# renders a human table via python-tabulate, whose column layout is not a
# stable interface; `protonvpn countries` is a short flat list, and
# `protonvpn connect --country XX` then lets Proton choose the least-loaded
# server in that country -- which is also what you actually want.
#
# Use the GTK app (`protonvpn-app` in the menu) for Secure Core / P2P / Tor or
# to pin one specific server; this picker is the fast path.
set -uo pipefail

export PATH="/run/current-system/sw/bin:/etc/profiles/per-user/$(id -un)/bin:$PATH"

notify() { command -v notify-send >/dev/null 2>&1 && notify-send "$@" || true; }
log()    { logger -t protonvpn-picker -- "$*" 2>/dev/null || true; }

# Signal waybar so the module redraws immediately instead of waiting out its
# poll interval. RTMIN+1 is unused elsewhere in this config.
refresh_bar() { pkill -RTMIN+1 waybar 2>/dev/null || true; }

if ! command -v protonvpn >/dev/null 2>&1; then
  notify -u critical "ProtonVPN" "protonvpn CLI not found on PATH"
  exit 1
fi

# `countries` needs an authenticated session; surface that instead of showing
# an empty picker.
if ! countries=$(protonvpn countries 2>&1); then
  log "countries failed: $countries"
  notify -u critical "ProtonVPN" "Not signed in — run 'protonvpn signin' in a terminal"
  exit 0
fi

# Keep only lines that look like "<CODE>  <Name>" and present "CODE — Name".
menu=$(printf '%s\n' "$countries" \
  | sed -n 's/^[[:space:]]*\([A-Z][A-Z]\)[[:space:]]\+\(.\+[^[:space:]]\)[[:space:]]*$/\1 — \2/p')

if [ -z "$menu" ]; then
  log "no countries parsed from: $countries"
  notify -u critical "ProtonVPN" "Couldn't parse the country list (see journalctl -t protonvpn-picker)"
  exit 0
fi

sel=$(printf '%s\n' "$menu" | tofi --prompt-text "ProtonVPN: " 2>/dev/null) || exit 0
[ -n "${sel:-}" ] || exit 0

code=${sel%% *}
log "connecting --country $code"
refresh_bar # show the "connecting" state right away

if out=$(protonvpn connect --country "$code" 2>&1); then
  notify -i network-vpn "ProtonVPN" "Connected — ${sel}"
else
  log "connect failed: $out"
  notify -u critical "ProtonVPN" "Connect failed: ${out:-unknown error}"
fi
refresh_bar
