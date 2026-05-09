#!/usr/bin/env bash
# Emit JSON for waybar's custom/services module: count of running services
# rendered as a gear-prefixed number, with a tooltip listing each unit.
set -euo pipefail

# Each entry: "<label>|<scope>|<primary unit>"
services=(
  "Immich|system|immich-server.service"
  "Syncthing|system|syncthing.service"
  "ActivityWatch|user|aw-server.service"
  "Tailscale|system|tailscaled.service"
)

state_of() {
  local scope="$1" unit="$2"
  local sctl=(systemctl)
  [ "$scope" = user ] && sctl=(systemctl --user)
  if "${sctl[@]}" is-active --quiet "$unit"; then echo on; else echo off; fi
}

active=0; total=${#services[@]}
tooltip_lines=()

for entry in "${services[@]}"; do
  IFS='|' read -r label scope unit <<<"$entry"
  st=$(state_of "$scope" "$unit")
  [ "$st" = on ] && active=$((active+1))
  tooltip_lines+=("$label: $st")
done
tooltip_lines+=("$active/$total running")

if [ "$active" -eq 0 ]; then class="none"
elif [ "$active" -eq "$total" ]; then class="all"
else class="some"
fi

text="⚙ $active"
tooltip=$(IFS=$'\n'; printf '%s' "${tooltip_lines[*]}")

# Build JSON by hand: waybar's systemd PATH is minimal and doesn't include jq.
json_escape() {
  local s="$1"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  printf '%s' "$s"
}
printf '{"text":"%s","tooltip":"%s","class":"%s"}\n' \
  "$(json_escape "$text")" \
  "$(json_escape "$tooltip")" \
  "$(json_escape "$class")"
