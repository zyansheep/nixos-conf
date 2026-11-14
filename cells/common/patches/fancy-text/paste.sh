#!/usr/bin/env bash
# fancy-text-paste TEXT — lands TEXT in the focused app the way emoji-picker
# does (cells/home/profiles/_files/emoji-picker.sh has the why): both
# selections, then Shift+Insert once niri has given focus back to the window
# Vicinae was opened over. The text stays on the clipboard for a manual paste.
set -euo pipefail
text=$1

printf '%s' "$text" | wl-copy --type 'text/plain;charset=utf-8'
printf '%s' "$text" | wl-copy --primary --type 'text/plain;charset=utf-8'

# niri, like vicinae, comes from PATH to match the compositor.
for _ in $(seq 25); do
  focused=$(niri msg -j focused-window 2>/dev/null) || {
    sleep 0.2
    break
  }
  [ "$focused" != null ] && break
  sleep 0.02
done

wtype -M shift -P Insert -p Insert -m shift || true
