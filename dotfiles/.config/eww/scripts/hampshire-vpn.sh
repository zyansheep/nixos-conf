#!/usr/bin/env bash
# Toggle the Hampshire (Fortinet/SAML) VPN from the eww services popup.
#
# How it connects: this gateway won't do openfortivpn's SAML->localhost
# redirect, so we reuse your *portal* login. svpncookie.js pulls the (HttpOnly,
# session) SVPNCOOKIE out of floorp's sessionstore; the root `hampshire-vpn
# connect` wrapper stashes it in a root-only /run file and (re)starts
# hampshire-vpn.service, which reads it via StandardInput=file:.
#
# Rules that shape this script (all learned the hard way):
#  1. eww KILLS onclick commands at :timeout (default 200ms!) — handlers must
#     never do slow work inline. `toggle` only picks a direction; the slow
#     parts (node over a 4MB sessionstore, sudo, systemctl) run in a detached
#     worker (setsid -f).
#  2. No hand-rolled state. The slider maps 1:1 onto `systemctl is-active`
#     (inactive/activating/active/deactivating/failed), same as the Tailscale
#     row. Attempt history: journalctl -u hampshire-vpn.
#  3. Every action logs to the journal (logger -t hampshire-toggle) — a click
#     must NEVER fail invisibly. `status` does not log (it polls every 1s).
#  4. Never auto-open the portal *logout* URL on a mere extraction miss — that
#     kills the session the user just created. Logout is only appropriate when
#     the gateway itself rejected the cookie (unit `failed`).
set -uo pipefail

# eww spawns this with a stripped PATH (/run/current-system/sw/bin only);
# restore the setuid sudo wrapper + per-user profile (node, xdg-open, ...).
export PATH="/run/wrappers/bin:/etc/profiles/per-user/$(id -un)/bin:$HOME/.nix-profile/bin:$PATH"

SUDO=/run/wrappers/bin/sudo
UNIT=hampshire-vpn.service
PORTAL="https://vpn.hampshire.edu:10443"
COOKIEJS="$HOME/.config/eww/scripts/svpncookie.js"
FAILMARK="${XDG_RUNTIME_DIR:-/tmp}/hampshire-vpn.notified" # notified-once guard

log()        { logger -t hampshire-toggle -- "$*" 2>/dev/null || true; }
unit_state() { systemctl is-active "$UNIT" 2>/dev/null || true; }
notify()     { command -v notify-send >/dev/null 2>&1 && notify-send "$@" || true; }

status() {
  local s
  s=$(unit_state)
  case "$s" in
    active) rm -f "$FAILMARK" ;;
    failed)
      # Gateway rejected the cookie / tunnel died: the session is genuinely
      # stale, so opening the logout page for a fresh sign-in is correct here
      # (and only here). Notify once per episode; slider stays red until the
      # next attempt.
      if [ ! -f "$FAILMARK" ]; then
        touch "$FAILMARK"
        log "status: unit failed -> notifying (see journalctl -u $UNIT)"
        setsid -f "$0" notify-failure >/dev/null 2>&1 </dev/null
      fi
      ;;
  esac
  echo "${s:-inactive}"
}

disconnect() {
  if "$SUDO" -n hampshire-vpn down; then
    log "down: dispatched"
  else
    log "down: sudo FAILED rc=$?"
    notify -u critical "Hampshire VPN" "Couldn't stop tunnel (sudo error)"
  fi
}

# The slow path — only ever runs detached from an eww handler (or in a
# terminal, via `up`).
worker() {
  local cookie="" out i
  # floorp flushes session cookies to sessionstore only every ~15s, so a click
  # right after logging into the portal may race the write — poll a bit.
  for i in $(seq 1 12); do
    cookie=$(node "$COOKIEJS" 2>/dev/null || true)
    [ -n "$cookie" ] && break
    sleep 2
  done
  log "worker: cookie len=${#cookie} (after $i probe(s))"
  if [ -z "$cookie" ]; then
    xdg-open "$PORTAL" >/dev/null 2>&1 &
    notify -u critical "Hampshire VPN" \
      "No portal session found — log in at vpn.hampshire.edu:10443 (School container), wait ~15s, then toggle again."
    return 0
  fi
  if out=$(printf '%s\n' "$cookie" | "$SUDO" -n hampshire-vpn connect 2>&1); then
    log "worker: connect dispatched to systemd"
    rm -f "$FAILMARK" # fresh attempt underway: re-arm the failure notice
  else
    log "worker: connect FAILED: ${out:-<no output>}"
    # The unit never took over (sudo denied, systemctl error, ...) — the
    # status poll can't see this kind of failure, so report it directly.
    notify -u critical "Hampshire VPN" "Couldn't start tunnel: ${out:-unknown sudo error}"
  fi
}

notify_failure() {
  local ctr
  ctr=$(node "$COOKIEJS" container 2>/dev/null || true)
  xdg-open "$PORTAL/remote/logout" >/dev/null 2>&1 &
  notify -u critical "Hampshire VPN" \
    "VPN connect failed (journalctl -u hampshire-vpn) — sign out & back in${ctr:+ (your $ctr container)}, then toggle again."
}

case "${1:-toggle}" in
  status) status ;; # no logging — polled every second
  down)   log "down: requested"; disconnect ;;
  up)     log "up: foreground worker"; worker ;; # nice for terminal debugging
  toggle)
    s=$(unit_state)
    log "toggle: state=$s"
    # Same semantics as toggle.sh: treat in-flight transitions as their
    # target, so a click during `activating` cancels and a click during
    # `deactivating` reconnects.
    case "$s" in
      active|activating) disconnect ;;
      *)
        setsid -f "$0" worker >/dev/null 2>&1 </dev/null
        log "toggle: worker spawn rc=$?"
        ;;
    esac
    ;;
  worker)         worker ;; # internal: detached connect
  notify-failure) notify_failure ;;
  *) echo "usage: hampshire-vpn.sh {status|up|down|toggle}" >&2; exit 1 ;;
esac
