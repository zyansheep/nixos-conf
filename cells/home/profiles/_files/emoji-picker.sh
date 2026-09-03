#!/usr/bin/env bash
# emoji-picker — rofimoji that actually lands the emoji in the focused app.
#
# rofimoji's default `type` action shells out to wtype, which fabricates a
# throwaway xkb keymap holding the chosen glyphs, uploads it over the
# virtual-keyboard protocol and immediately "presses" the new keys. Clients
# that re-read the keymap promptly (foot) get the right bytes; Electron and
# Firefox — vesktop, Signal, floorp, i.e. most of what emoji get typed into —
# either drop the events or decode them against the keymap they still have
# cached and emit garbage. That is the "nothing happened" / "broken character"
# split, and it's a client-side race no delay inside rofimoji can close.
#
# So don't type the glyph at all. Put it on both selections and paste with
# Shift+Insert: an ordinary keysym every app already has in its real keymap,
# so no keymap upload and nothing to race. GTK/Electron read CLIPBOARD from
# it, terminals read PRIMARY — hence seeding both with the same text.
#
# Unlike `rofimoji --action clipboard`, the emoji is deliberately LEFT on the
# clipboard rather than restored, so a manual Ctrl+V/Ctrl+Shift+V still works
# in any app that ignores the synthetic paste.
#
# Set EMOJI_PICKER_NO_PASTE=1 to skip the paste and only copy.
set -euo pipefail

# --action print keeps rofimoji out of the insertion business entirely; it just
# hands back the selection. Extra args land after ours, so `--files ...` from
# the caller overrides the default set.
chars=$(rofimoji --files emojis kaomoji --action print "$@") || exit 0
[ -n "$chars" ] || exit 0

printf '%s' "$chars" | wl-copy
printf '%s' "$chars" | wl-copy --primary

[ -n "${EMOJI_PICKER_NO_PASTE:-}" ] && exit 0

# rofi's layer surface holds an exclusive keyboard grab; niri only restores
# focus to the previous window once that surface is torn down. Pasting into
# that gap is the other way keystrokes get swallowed.
sleep 0.2

# Never fail the picker over a refused paste — the clipboard still has it.
wtype -M shift -P Insert -p Insert -m shift || true
