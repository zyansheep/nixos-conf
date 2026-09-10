#!/usr/bin/env bash
# JSON for waybar's custom/protonvpn module.
#
# The module is HIDDEN whenever Proton is disconnected: this script emits an
# empty `text` and the module sets `hide-empty-text: true`. That option (waybar
# >= 0.15) is required rather than merely nice -- the module's `format` carries
# a static shield glyph, and without it waybar would keep drawing the icon on
# its own.
#
# State comes from NetworkManager, not from an interface existence check. NM's
# active-connection list is real liveness: a Proton certificate that expires
# tears the connection down and it leaves this list. (An `ip link` style check
# would have kept reporting "up" on a dead tunnel -- the exact silent-failure
# mode that ruled out a hand-maintained wg-quick config.)
set -uo pipefail

# waybar's systemd unit has a minimal PATH and no jq.
export PATH="/run/current-system/sw/bin:$PATH"

# -t = terse, colon-separated, stable across NM versions; the human-readable
# `nmcli con show` table is not safe to parse.
active=$(nmcli -t -f NAME,TYPE,DEVICE,STATE connection show --active 2>/dev/null || true)

# Proton's client creates the tunnel as a wireguard connection (protun /
# wireguard protocols) or, on the openvpn protocol, as type `vpn`.
row=$(printf '%s\n' "$active" \
  | awk -F: '$2 == "wireguard" || $2 == "vpn" { print; exit }')

json_escape() {
  local s="$1"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  printf '%s' "$s"
}

emit() { # text tooltip class
  printf '{"text":"%s","tooltip":"%s","class":"%s"}\n' \
    "$(json_escape "$1")" "$(json_escape "$2")" "$(json_escape "$3")"
}

if [ -z "$row" ]; then
  # Disconnected -> empty text -> module disappears from the bar.
  emit "" "" "off"
  exit 0
fi

IFS=: read -r name _type dev state <<<"$row"

# Strip Proton's connection-name prefix so the bar shows "NL#42", not
# "ProtonVPN NL#42". Falls back to the raw name if the prefix ever changes.
label=${name#ProtonVPN }
label=${label#Proton VPN }

hint='Click to switch server · Right-click to disconnect'

# Show the module while the tunnel is still coming up, so it appears the
# instant the switch is flipped rather than only once the handshake lands.
if [ "$state" != "activated" ]; then
  emit "…" "ProtonVPN: connecting to ${label}"$'\n'"$hint" "connecting"
  exit 0
fi

tooltip="ProtonVPN: ${name}"
[ -n "${dev:-}" ] && tooltip="${tooltip}"$'\n'"Interface: ${dev}"
emit "$label" "${tooltip}"$'\n'"$hint" "connected"
