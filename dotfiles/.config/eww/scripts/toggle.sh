#!/usr/bin/env bash
# Toggle a service group from the eww popup.
# Decision is based on the current systemd state, treating in-flight
# transitions as their intended target so a click during a slow
# `activating` / `deactivating` does the user's expected thing
# (cancel the in-progress transition).
#
# Visual feedback is handled entirely by the eww defpoll on the service
# state — this script just kicks off the systemctl call and exits.
set -uo pipefail

group="${1:?usage: toggle.sh <group>}"
# scope=system → systemctl; scope=user → systemctl --user
case "$group" in
  immich)        scope=system; primary="immich-server.service" ;;
  syncthing)     scope=system; primary="syncthing.service"      ;;
  activitywatch) scope=user;   primary="aw-server.service"      ;;
  *) echo "unknown group: $group" >&2; exit 1 ;;
esac

sctl=(systemctl)
[ "$scope" = user ] && sctl=(systemctl --user)

state=$("${sctl[@]}" is-active "$primary" 2>/dev/null || true)
case "$state" in
  active|activating) action=stop ;;
  *)                 action=start ;;
esac

# Fire and forget. svc-toggle.sh handles the multi-unit groups (e.g. immich's
# server + ml + redis) and all error reporting. Backgrounded so a slow
# systemctl stop doesn't keep this script alive.
~/.config/waybar/scripts/svc-toggle.sh "$action" "$group" >/dev/null 2>&1 &
disown
