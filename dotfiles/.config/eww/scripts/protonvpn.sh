#!/usr/bin/env bash
# ProtonVPN row for the eww services popup, and the disconnect path used by
# the waybar module's right-click.
#
# Rules inherited from hampshire-vpn.sh (learned the hard way, see its header):
#  1. eww KILLS onclick commands at :timeout. `toggle` only decides a direction
#     and hands the slow work (a Proton API round-trip can take seconds) to a
#     detached worker via `setsid -f`.
#  2. No hand-rolled state. The slider maps onto NetworkManager's
#     active-connection state, which is real liveness -- an expired Proton
#     certificate drops the connection and the row goes grey on its own.
#  3. Every action logs; a click must never fail invisibly. `status` does not
#     log (it is polled).
#
# Unlike hampshire-vpn this needs NO sudo: NM's polkit rules let members of
# the `networkmanager` group manage connections, and profiles/services/
# protonvpn.nix puts zyansheep in it.
set -uo pipefail

export PATH="/run/current-system/sw/bin:/etc/profiles/per-user/$(id -un)/bin:$PATH"

log()    { logger -t protonvpn-toggle -- "$*" 2>/dev/null || true; }
notify() { command -v notify-send >/dev/null 2>&1 && notify-send "$@" || true; }
refresh_bar() { pkill -RTMIN+1 waybar 2>/dev/null || true; }

# Emit the eww switch CSS classes (active/activating/inactive), derived from
# NM rather than invented here.
status() {
  local row state
  row=$(nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null \
        | awk -F: '$2 == "wireguard" || $2 == "vpn" { print; exit }')
  if [ -z "$row" ]; then echo inactive; return; fi
  state=$(printf '%s' "$row" | awk -F: '{print $3}')
  case "$state" in
    activated) echo active ;;
    *)         echo activating ;;
  esac
}

disconnect() {
  refresh_bar
  if out=$(protonvpn disconnect 2>&1); then
    log "disconnect: ok"
    notify -i network-offline "ProtonVPN" "Disconnected"
  else
    log "disconnect FAILED: $out"
    notify -u critical "ProtonVPN" "Disconnect failed: ${out:-unknown error}"
  fi
  refresh_bar
}

# Slow path -- only ever runs detached from an eww handler.
worker() {
  refresh_bar
  if out=$(protonvpn connect 2>&1); then
    log "connect: ok (fastest server)"
    notify -i network-vpn "ProtonVPN" "Connected"
  else
    log "connect FAILED: $out"
    # Most common cause by far: no authenticated session yet.
    case "$out" in
      *"sign in"*|*"signin"*|*"not logged"*|*"Unauthorized"*|*"401"*)
        notify -u critical "ProtonVPN" "Not signed in — run 'protonvpn signin' in a terminal" ;;
      *)
        notify -u critical "ProtonVPN" "Connect failed: ${out:-unknown error}" ;;
    esac
  fi
  refresh_bar
}

case "${1:-toggle}" in
  status) status ;; # no logging: polled
  down)   log "down: requested"; disconnect ;;
  up)     log "up: foreground worker"; worker ;; # terminal debugging
  worker) worker ;;                              # internal: detached connect
  toggle)
    s=$(status)
    log "toggle: state=$s"
    # Treat an in-flight transition as its target, so a click during
    # `activating` cancels -- same semantics as toggle.sh.
    case "$s" in
      active|activating) disconnect ;;
      *) setsid -f "$0" worker >/dev/null 2>&1 </dev/null; log "toggle: worker spawn rc=$?" ;;
    esac
    ;;
  *) echo "usage: protonvpn.sh {status|up|down|toggle}" >&2; exit 1 ;;
esac
