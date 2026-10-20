# Jisho: a Japanese dictionary in Vicinae

A Vicinae extension (the launcher behind Mod+D, Mod+C and Mod+;) with one
search bar for everything:

| Type | Get |
|---|---|
| English (`eat`, `rice ball`) | Japanese words whose meaning it is, the usual one first |
| Japanese (`ねこ`, `猫`, `ネコ`) | the word, then words starting so |
| A conjugated word (`食べさせられなかった`) | 食べる, as "causative · potential or passive · negative · past" |
| Romaji (`taberu`, `konnichiwa`) | the word, as an IME would read it |
| A sentence, in either language | an offline translation, and the words it's made of |
| A kanji, or an English meaning (`water`) | the kanji: readings, strokes in order, words with it |

**Mod+'** opens it; **Mod+Shift+'** looks up the selected
text, or else the clipboard: `jisho-lookup`, in dotfiles/.config/niri/config.kdl.

Each word's page has its reading over it, romaji, pitch accent (high morae
overlined, ꜜ where it drops), JLPT level and frequency rank, every sense with
its part of speech, notes and Tatoeba example sentences, other spellings, a
verb's or adjective's common forms, and the kanji it's written with.

| Key | On a word | On the translation | On a kanji |
|---|---|---|---|
| Enter | copy the word | copy the translation | copy the kanji |
| Ctrl+R | copy the reading | | |
| Ctrl+Shift+C | copy word, reading and meanings | copy the original | copy the meanings |
| Ctrl+T | conjugation table | | |
| Ctrl+Shift+K | its kanji | | |
| Ctrl+W | | | words with it |
| Ctrl+S | | translate the other way | |
| Ctrl+O | open on jisho.org | | open on jisho.org |
| Ctrl+G / Ctrl+D | | open in Google Translate / DeepL | |

Ctrl+B is Vicinae's action menu, which lists these and more ("Look up" the
related words, Tatoeba). Its vim-style keys (Ctrl+J/K/H/L/N/P) are left alone.

## Translation

Offline, private and about 0.4 s a sentence: translateLocally (Bergamot)
running Mozilla's Firefox Translations models, the ones Firefox ships
(ja→en: the desktop release, `base`; en→ja: `base-memory`). Good for the gist;
for anything that matters, the Google Translate and DeepL actions send the
text to them, and only then. A sentence is translated as you type; a single
word's translation row waits until you select it.

## Data

Built into one SQLite database (`build_db.py`, about 140 MB) from pinned
releases in cells/common/packages/jisho.nix:

- JMdict with Tatoeba examples and KANJIDIC2, KRADFILE (EDRDG, CC BY-SA 4.0),
  through [jmdict-simplified](https://github.com/scriptin/jmdict-simplified)'s JSON.
- [KanjiVG](https://kanjivg.tagaini.net) stroke order (CC BY-SA 3.0),
  redrawn with each stroke and its number on a colour ramp, first to last.
- [JPDB](https://jpdb.io)'s frequency ranks (anime, dramas, novels), as
  [Yomitan dictionaries](https://github.com/Kuuuube/yomitan-dictionaries).
- Jonathan Waller's JLPT lists, via
  [open-anki-jlpt-decks](https://github.com/jamsinclair/open-anki-jlpt-decks) (CC BY).
- [kanjium](https://github.com/mifunetoshiro/kanjium)'s pitch accents (CC BY-SA 4.0).

Ranking: a match's kind (the exact word or gloss; inflected; a prefix or a
gloss with the words) plus how rare the word is (8·log₁₀ of its rank) plus,
for English, how far into the entry the gloss is. So "eat" gives 食べる
before 食う, and "head" 頭 before 行く's "to head (towards)".

Inflections come from one table of stems (`src/lib/inflect.ts`) that both
`deinflect`s (rules run backwards while each step's class fits the next, so a
chain only passes through real forms) and builds the conjugation tables; the
tests check every table cell deinflects back.

## Building and testing

`nix build .#nixosConfigurations.isomorph.pkgs.jisho` bundles `src/search.tsx`
with esbuild, `react` and `@vicinae/api` left to Vicinae's runtime as
`vici build` does, and runs `node --test test/*.test.ts` against the real
database and translator. Outside Nix the database tests skip unless
`JISHO_DB` names a database, and the translation one unless
`JISHO_TRANSLATOR` and `JISHO_MODELS` are set.

The home profile (cells/home/profiles/jisho.nix) links the extension into
~/.local/share/vicinae/extensions/jisho. Vicinae only watches that directory if
it existed when the server started, so restart `vicinae.service` the first time.

Vicinae notes, from building this (0.27.4):

- A command gets a deadline to come up, counted while the server builds its
  view; the first one after a start compiles the QML views, which on battery
  took over the stock 1 s, and a miss unloads the command (an "Extension
  crashed" screen 5 s later). niri.nix patches it to 5 s
  (../vicinae/extension-handshake-timeout.patch). The command's own first
  render is kept trivial too.
- The list resets its selection on the first render after a search event, so
  `search.tsx` renders on every one, and moving the selection never renders
  (save the translation row's first selection).
- The detail pane is markdown rendered as Qt rich text: inline `<span>`s with
  colour, size and `text-decoration:overline` work, a line that is only an
  `<img>` becomes an image (SVG data URIs too), GFM tables and `> [!TIP]`
  callouts work; HTML blocks lose their tags, and there is no ruby.
