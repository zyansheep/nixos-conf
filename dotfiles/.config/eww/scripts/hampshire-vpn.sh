#!/usr/bin/env bash
# Toggle the Hampshire (Fortinet/SAML) VPN from the eww services popup.
#
# Why this isn't just `sudo openfortivpn`: openfortivpn must run as root (tun +
# routes), but a root process can't reach your Wayland/DBus session to open
# Firefox for the SAML login. So we split it: `sudo hampshire-vpn up` runs the
# tunnel as root (nopass, scoped — see profiles/services/hampshire-vpn.nix) and
# logs to a file; this user-session script tails that log for the SAML URL and
# opens it in your browser itself.
#
# status -> active|inactive (consumed by the eww defpoll switch)
# down   -> stop the tunnel
# toggle -> stop if up, otherwise connect (default action for the popup button)
set -uo pipefail

# eww spawns this from waybar's restricted systemd env, so PATH is just
# /run/current-system/sw/bin. That stripped PATH breaks two things, so repair it:
#  - bare `sudo` resolves to the NON-setuid sudo-rs copy ("sudo must be owned by
#    uid 0…"); we also call the setuid wrapper by absolute path ($SUDO) to be safe.
#  - xdg-open validates a handler's Exec is runnable, so it SKIPS floorp
#    (`Exec=floorp`, a bare command not on this PATH) and falls back to brave
#    (`Exec=/nix/store/…/brave`, absolute) — that's why SAML opened in Brave.
#    Restoring the per-user profile bin lets xdg-open resolve the real default.
export PATH="/run/wrappers/bin:/etc/profiles/per-user/$(id -un)/bin:$HOME/.nix-profile/bin:$PATH"

LOG="${XDG_RUNTIME_DIR:-/tmp}/hampshire-vpn.log"
SUDO=/run/wrappers/bin/sudo

is_up() { pgrep -x openfortivpn >/dev/null 2>&1; }

connect() {
  is_up && return 0
  : >"$LOG"
  # Detach the tunnel; root writes into the fd we (the user) opened, so the log
  # stays user-readable. setsid frees it from eww's process group.
  setsid "$SUDO" hampshire-vpn up >"$LOG" 2>&1 &
  disown
  # Watch for the gateway SAML URL and open it in the browser.
  setsid bash -c '
    log="'"$LOG"'"
    for _ in $(seq 1 60); do
      url=$(grep -oE "https://[^[:space:]]+" "$log" 2>/dev/null \
              | grep -m1 -i saml \
            || grep -oE "https://[^[:space:]]+" "$log" 2>/dev/null | head -n1)
      if [ -n "$url" ]; then
        xdg-open "$url" >/dev/null 2>&1
        command -v notify-send >/dev/null 2>&1 \
          && notify-send "Hampshire VPN" "Opening SAML login in your browser…"
        exit 0
      fi
      sleep 1
    done
    command -v notify-send >/dev/null 2>&1 \
      && notify-send -u critical "Hampshire VPN" "No SAML URL appeared — check $log"
  ' >/dev/null 2>&1 &
  disown
}

case "${1:-toggle}" in
  status) is_up && echo active || echo inactive ;;
  down)   "$SUDO" hampshire-vpn down ;;
  up)     connect ;;
  toggle) if is_up; then "$SUDO" hampshire-vpn down; else connect; fi ;;
  *)      echo "usage: hampshire-vpn.sh {status|up|down|toggle}" >&2; exit 1 ;;
esac
