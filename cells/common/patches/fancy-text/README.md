# Fancy Text: cool Unicode text styles in Vicinae

A Vicinae extension that works like [lingojam's Cool Text Fonts](https://lingojam.com/CoolTextFonts):
type something and every style shows it at once (cursive, double-struck,
bubbles, upside down, vaporwave, creepy, framed in ★彡 … 彡★, 84 in all).
**Mod+Shift+;** opens it, next to the emoji picker on Mod+; (dotfiles/.config/niri/config.kdl).

| Key | Does |
|---|---|
| Enter | paste it into the app you were in (and leave it on the clipboard) |
| Ctrl+Shift+C | only copy it |
| Ctrl+R | re-roll the random styles (marked with the shuffle icon) |
| Ctrl+E | edit your own styles |

Styles you use go to the top under Recent. With nothing typed, each style
shows its own name.

Pasting goes the emoji picker's way (`paste.sh`, installed as
`fancy-text-paste`): both selections, then Shift+Insert once niri has given
focus back, since Vicinae's own paste needs its input server.

## Your own styles

`~/.config/fancy-text/styles.json` (Ctrl+E starts it with examples), a list of:

```json
{ "name": "Sparkly bold", "base": "Bold", "frame": "✧･ﾟ: *✧･ﾟ:* {text} *:･ﾟ✧*:･ﾟ✧" }
```

Every step is optional, applied in this order: `base` (another style's name),
`from`+`to` (alphabets matched character by character) or `map` (an object),
`mark` (a combining character after each one, like `"̅"`), `join`, and
`frame` (with `{text}`). They're read when the command opens; a broken file
shows a toast saying what's wrong.

## Where the styles come from

`src/lingojam.json` is the data from lingojam.com/CoolTextFonts' page script
(its character maps, the Lunicode maps it uses for tiny, flipped, mirrored,
bubble and creepy text, its frames, kaomoji and emoji), dumped once by running
that script. `src/styles.ts` re-implements what lingojam does with it, but on
code points (so emoji in the input survive) and with a seeded random, so the
random styles stay put while you type until Ctrl+R.

`node --no-warnings --test test/*.test.ts` runs the tests (the build does too).
