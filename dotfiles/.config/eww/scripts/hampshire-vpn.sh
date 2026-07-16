#!/usr/bin/env bash
# Toggle the Hampshire (Fortinet/SAML) VPN from the eww services popup.
#
# How it connects: this gateway won't do openfortivpn's SAML->localhost
# redirect, so we reuse your *portal* login. svpncookie.js pulls the (HttpOnly,
# session) SVPNCOOKIE out of floorp's sessionstore; the root `hampshire-vpn
# connect` wrapper stashes it in a root-only /run file and (re)starts
# hampshire-vpn.service, which reads it via StandardInput=file:.
#
# Two hard-won rules shape this script:
#  1. eww KILLS onclick commands at :timeout (default 200ms!) — handlers must
#     never do slow work inline. `toggle` only picks a direction; the slow
#     parts (node over a 4MB sessionstore, sudo, systemctl) run in a detached
#     worker (setsid -f).
#  2. No hand-rolled state. The slider maps 1:1 onto `systemctl is-active`
#     (inactive/activating/active/deactivating/failed), same as the Tailscale
#     row. Attempt history: journalctl -u hampshire-vpn.
set -uo pipefail

# eww spawns this with a stripped PATH (/run/current-system/sw/bin only);
# restore the setuid sudo wrapper + per-user profile (node, xdg-open, ...).
export PATH="/run/wrappers/bin:/etc/profiles/per-user/$(id -un)/bin:$HOME/.nix-profile/bin:$PATH"

SUDO=/run/wrappers/bin/sudo
UNIT=hampshire-vpn.service
PORTAL="https://vpn.hampshire.edu:10443"
COOKIEJS="$HOME/.config/eww/scripts/svpncookie.js"
FAILMARK="${XDG_RUNTIME_DIR:-/tmp}/hampshire-vpn.notified" # notified-once guard

unit_state() { systemctl is-active "$UNIT" 2>/dev/null || true; }
notify()     { command -v notify-send >/dev/null 2>&1 && notify-send "$@" || true; }

# Point the user at the portal to refresh the login (SVPNCOOKIE is minted at
# portal sign-in). Names the Firefox container holding the old cookie, since
# xdg-open lands in the default container, not that one.
open_portal() {
  local why="$1" ctr
  ctr=$(node "$COOKIEJS" container 2>/dev/null || true)
  xdg-open "$PORTAL/remote/logout" >/dev/null 2>&1 &
  notify -u critical "Hampshire VPN" \
    "$why — sign out & back in${ctr:+ (your $ctr container)}, then toggle again."
}

status() {
  local s
  s=$(unit_state)
  case "$s" in
    active) rm -f "$FAILMARK" ;;
    failed)
      # Unit-level failure (cookie rejected, tunnel died, ...): notify once
      # per episode. The slider honestly stays red until the next attempt.
      if [ ! -f "$FAILMARK" ]; then
        touch "$FAILMARK"
        setsid -f "$0" notify-failure >/dev/null 2>&1 </dev/null
      fi
      ;;
  esac
  echo "${s:-inactive}"
}

disconnect() {
  "$SUDO" -n hampshire-vpn down \
    || notify -u critical "Hampshire VPN" "Couldn't stop tunnel (sudo error)"
}

# The slow path — only ever runs detached from an eww handler (or in a
# terminal, via `up`).
worker() {
  local cookie out
  cookie=$(node "$COOKIEJS" 2>/dev/null || true)
  if [ -z "$cookie" ]; then
    open_portal "No portal session found in floorp"
    return 0
  fi
  if out=$(printf '%s\n' "$cookie" | "$SUDO" -n hampshire-vpn connect 2>&1); then
    rm -f "$FAILMARK" # fresh attempt underway: re-arm the failure notice
  else
    # The unit never took over (sudo denied, systemctl error, ...) — the
    # status poll can't see this kind of failure, so report it directly.
    notify -u critical "Hampshire VPN" "Couldn't start tunnel: ${out:-unknown sudo error}"
  fi
}

case "${1:-toggle}" in
  status) status ;;
  down)   disconnect ;;
  up)     worker ;; # foreground: nice for terminal debugging
  toggle)
    # Same semantics as toggle.sh: treat in-flight transitions as their
    # target, so a click during `activating` cancels and a click during
    # `deactivating` reconnects.
    case "$(unit_state)" in
      active|activating) disconnect ;;
      *)                 setsid -f "$0" worker >/dev/null 2>&1 </dev/null ;;
    esac
    ;;
  worker)         worker ;; # internal: detached connect
  notify-failure) open_portal "VPN connect failed (details: journalctl -u hampshire-vpn)" ;;
  *) echo "usage: hampshire-vpn.sh {status|up|down|toggle}" >&2; exit 1 ;;
esac
