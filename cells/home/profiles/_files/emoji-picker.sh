#!/usr/bin/env bash
# emoji-picker — emoji, kaomoji and math symbols in Vicinae, landed in the
# focused app.
#
# The list is $EMOJI_PICKER_DATA, built by ../emoji.nix: one
# `value<TAB>display` line per entry. Vicinae's dmenu shows and fuzzy-matches
# the display column (names and keywords included) and answers with the
# chosen display line, which maps back to its value.
#
# Vicinae's own emoji grid can paste, but only through its uinput input
# server (disabled here), it has no kaomoji, and synthesising the glyph as
# keystrokes the way wtype does (rofimoji's old `type` action) loses or
# garbles it in Electron and Firefox: wtype uploads a throwaway keymap and
# presses the new keys before those clients have re-read it.
#
# So don't type the glyph at all. Put it on both selections and paste with
# Shift+Insert: an ordinary keysym every app already has in its real keymap,
# so no keymap upload and nothing to race. GTK/Electron read CLIPBOARD from
# it, terminals read PRIMARY — hence seeding both with the same text. It is
# deliberately LEFT on the clipboard, so a manual Ctrl+V/Ctrl+Shift+V still
# works in any app that ignores the synthetic paste.
#
# Recent picks are listed first (most recent at the top) from
# ~/.local/state/emoji-picker/recent.
#
# Set EMOJI_PICKER_NO_PASTE=1 to skip the paste and only copy.
set -euo pipefail

state=${XDG_STATE_HOME:-$HOME/.local/state}/emoji-picker
recent=$state/recent
mkdir -p "$state"
touch "$recent"

list=$(mktemp)
trap 'rm -f "$list"' EXIT

# Recent values first (dropping any the data no longer has), then the rest.
awk -F'\t' '
  NR == FNR { if ($0 != "" && !($0 in rank)) rank[$0] = ++n; next }
  $1 in rank { hit[rank[$1]] = $0; next }
  { rest[++m] = $0 }
  END {
    for (i = 1; i <= n; i++) if (i in hit) print hit[i]
    for (i = 1; i <= m; i++) print rest[i]
  }
' "$recent" "$EMOJI_PICKER_DATA" > "$list"

# The `vicinae` on PATH is the resident server's own CLI (niri.nix).
choice=$(cut -f2 "$list" | vicinae dmenu \
  --navigation-title "Emoji & Kaomoji" \
  --placeholder "Search emoji, kaomoji and math symbols…" \
  --section-title "{count} results" \
  --no-quick-look) || exit 0
[ -n "$choice" ] || exit 0

# (ENVIRON, not -v: awk would eat the backslash in ¯\_(ツ)_/¯.)
chars=$(CHOICE=$choice awk -F'\t' '$2 == ENVIRON["CHOICE"] { print $1; exit }' "$list")

if [ -n "$chars" ]; then
  { printf '%s\n' "$chars"; grep -vxF -e "$chars" "$recent" || true; } | awk 'NR <= 64' > "$recent.new"
  mv "$recent.new" "$recent"
else
  # Not a list entry: "Pass search text" answers with the query, inserted as
  # typed and not remembered.
  chars=$choice
fi

printf '%s' "$chars" | wl-copy
printf '%s' "$chars" | wl-copy --primary

[ -n "${EMOJI_PICKER_NO_PASTE:-}" ] && exit 0

# Vicinae's layer surface holds the keyboard until it is torn down, and niri
# only then hands focus back to the previous window. Pasting into that gap is
# the other way keystrokes get swallowed.
sleep 0.2

# Never fail the picker over a refused paste — the clipboard still has it.
wtype -M shift -P Insert -p Insert -m shift || true
