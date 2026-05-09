#!/usr/bin/env bash
# Toggle a managed service group on/off via systemctl, with notify-send feedback.
# Usage: svc-toggle.sh <action> <group>
#   action: toggle | start | stop | status
#   group:  immich | syncthing | activitywatch | tailscale
set -euo pipefail

action="${1:-toggle}"
group="${2:-}"

# Each group declares a scope (system|user) so the same plumbing can drive
# both system-level units (immich, syncthing) and per-user units (aw-*).
case "$group" in
  immich)
    label="Immich"
    scope=system
    units=(immich-server.service immich-machine-learning.service redis-immich.service)
    primary="immich-server.service"
    ;;
  syncthing)
    label="Syncthing"
    scope=system
    units=(syncthing.service)
    primary="syncthing.service"
    ;;
  activitywatch)
    label="ActivityWatch"
    scope=user
    units=(aw-server.service app-aw-qt@autostart.service)
    primary="aw-server.service"
    ;;
  tailscale)
    label="Tailscale"
    scope=system
    units=(tailscaled.service)
    primary="tailscaled.service"
    ;;
  *)
    notify-send -u critical "svc-toggle" "Unknown group: $group"
    exit 1
    ;;
esac

# Bind systemctl invocation to the right bus once.
sctl=(systemctl)
[ "$scope" = user ] && sctl=(systemctl --user)

is_active() { "${sctl[@]}" is-active --quiet "$primary"; }

run_systemctl() {
  local verb="$1"; shift
  if "${sctl[@]}" "$verb" "$@" 2>/tmp/svc-toggle.err; then
    return 0
  else
    notify-send -u critical "$label: $verb failed" "$(cat /tmp/svc-toggle.err)"
    return 1
  fi
}

case "$action" in
  status)
    if is_active; then echo "active"; else echo "inactive"; fi
    ;;
  start)
    run_systemctl start "${units[@]}" && \
      notify-send -i media-playback-start "$label started"
    ;;
  stop)
    run_systemctl stop "${units[@]}" && \
      notify-send -i media-playback-stop "$label stopped"
    ;;
  toggle)
    if is_active; then
      run_systemctl stop "${units[@]}" && \
        notify-send -i media-playback-stop "$label stopped"
    else
      run_systemctl start "${units[@]}" && \
        notify-send -i media-playback-start "$label started"
    fi
    ;;
  *)
    notify-send -u critical "svc-toggle" "Unknown action: $action"
    exit 1
    ;;
esac
