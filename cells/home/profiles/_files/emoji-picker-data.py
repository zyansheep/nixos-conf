"""Build the emoji-picker list: one `value<TAB>display` line per entry.

The value is what gets inserted; the display line is what Vicinae's dmenu
shows and fuzzy-matches, so it carries every searchable name and keyword.

  emoji    Unicode emoji-test.txt (fully-qualified only, no bare components),
           names from the file, keywords from CLDR's English annotations.
  kaomoji  the hand-made kaomoji.txt, grouped by mood; each group follows
           the emoji its header names, so the two are interleaved.
  math     Unicode MathClassEx (what rofimoji's `math` set was built from),
           with its entity and TeX names (rArr, /wedge, /land) as keywords.
"""

import argparse
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

# Vicinae's dmenu treats an entry starting with "/" as a file path and shows
# only its last component, which mangles kaomoji like /ᐠ｡ꞈ｡ᐟ\ — an invisible
# word joiner in front of the display text sidesteps that.
WORD_JOINER = "⁠"

# A control character anywhere in the dmenu input breaks Vicinae's IPC
# decoding of the rest of the request (the query, the options), so nothing
# from these categories reaches the list.
FORBIDDEN = {"Cc", "Cs", "Co", "Zl", "Zp"}


def clean(text):
    text = "".join(c for c in text if unicodedata.category(c) not in FORBIDDEN)
    return " ".join(text.split())


def emoji_key(char):
    """An emoji with its variation selectors dropped, so ☺ and ☺️ match."""
    return char.replace("️", "")


def cldr_keywords(paths):
    keywords = {}
    for path in paths:
        for node in ET.parse(path).getroot().iter("annotation"):
            if node.get("type") == "tts" or not node.text:
                continue
            words = [clean(w) for w in node.text.split("|")]
            keywords.setdefault(emoji_key(node.get("cp")), words)
    return keywords


def emoji_entries(test_path, keywords):
    line_re = re.compile(r"^[0-9A-F ]+;\s*fully-qualified\s*#\s*(\S+)\s+E\d+\.\d+\s+(.+)$")
    group = None
    for line in open(test_path, encoding="utf-8"):
        if line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
            continue
        m = line_re.match(line.rstrip("\n"))
        if not m or group == "Component":
            continue
        char, name = m.groups()
        name_words = set(re.findall(r"[\w'-]+", name.lower()))
        extra = [w for w in keywords.get(emoji_key(char), [])
                 if w.lower() != name.lower() and not set(w.lower().split()) <= name_words]
        yield char, name, extra


# --- kaomoji ---------------------------------------------------------------

def kaomoji_groups(path):
    """{emoji: [(kaomoji, mood, keywords), ...]} from kaomoji.txt:
    `## emoji mood · keywords` groups of `kaomoji` lines, each optionally
    followed by two spaces and keywords of its own, plus where each emoji was
    named. Mistakes fail the build rather than vanish."""
    groups, where = {}, {}
    mood, group_words, seen = None, [], set()
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.rstrip("\n")
        if not line.strip() or line == "#" or line.startswith("# "):
            continue

        def fail(why):
            sys.exit(f"{path}:{n}: {why}: {line!r}")

        if line.startswith("## "):
            head, _, words = line[3:].partition(" · ")
            anchor, _, mood = head.partition(" ")
            if not mood:
                fail("expected `## emoji mood · keywords`")
            anchor = emoji_key(anchor)
            where.setdefault(anchor, f"{path}:{n}")
            group = groups.setdefault(anchor, [])
            group_words = [w.strip() for w in words.split(",") if w.strip()]
            continue
        if mood is None:
            fail("kaomoji before the first `## emoji mood` line")
        if line[0].isspace():
            fail("leading whitespace")
        k, _, own = line.partition("  ")
        k = unicodedata.normalize("NFC", k)
        if any(unicodedata.category(c) in FORBIDDEN | {"Cn"} for c in k):
            fail("control or unassigned character")
        if k in seen:
            fail("listed twice")
        seen.add(k)
        own_words = [w.strip() for w in own.split(",") if w.strip()]
        # dict.fromkeys drops a word the group already has, keeping order.
        group.append((k, mood, list(dict.fromkeys(group_words + own_words))))
    return groups, where


# --- math ------------------------------------------------------------------

def unicode_names(path):
    names = {}
    for line in open(path, encoding="utf-8"):
        fields = line.split(";")
        if not fields[1].startswith("<"):
            names[int(fields[0], 16)] = fields[1]
    return names


def math_entries(path, names):
    extras = {}
    for line in open(path, encoding="utf-8"):
        if not line.strip() or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.rstrip("\n").split(";")]
        start, _, end = fields[0].partition("..")
        # The note mixes TeX names ("/wedge /land") with prose ("implies").
        tex = re.findall(r"/([A-Za-z]+)", fields[5])
        prose = clean(re.sub(r"/[A-Za-z]+", " ", fields[5]))
        for cp in range(int(start, 16), int(end or start, 16) + 1):
            words = extras.setdefault(cp, [])
            for word in [fields[3], *tex, prose]:
                if word and word not in words:
                    words.append(word)
    for cp, words in extras.items():
        if cp < 0x80 or cp not in names:  # ASCII is on the keyboard already
            continue
        yield chr(cp), names[cp].lower(), words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emoji-test", required=True)
    ap.add_argument("--cldr", action="append", default=[])
    ap.add_argument("--kaomoji", required=True)
    ap.add_argument("--math-class", required=True)
    ap.add_argument("--unicode-data", required=True)
    args = ap.parse_args()

    seen = set()
    counts = {}
    out = sys.stdout

    def emit(kind, value, title, extra):
        value = unicodedata.normalize("NFC", value.strip())
        if not value or value in seen:
            return
        seen.add(value)
        counts[kind] = counts.get(kind, 0) + 1
        # Only the description is whitespace-normalised: the value's own
        # spacing is part of the kaomoji, and keeps every display line unique.
        display = f"{value}  " + clean(title + (f" · {', '.join(extra)}" if extra else ""))
        if display.startswith("/"):
            display = WORD_JOINER + display
        out.write(f"{value}\t{display.rstrip()}\n")

    kaomoji, where = kaomoji_groups(args.kaomoji)
    for char, name, extra in emoji_entries(args.emoji_test, cldr_keywords(args.cldr)):
        emit("emoji", char, name, extra)
        for value, mood, keywords in kaomoji.pop(emoji_key(char), []):
            emit("kaomoji", value, mood, keywords)
    for anchor in kaomoji:
        sys.exit(f"{where[anchor]}: {anchor} is not in the emoji list")
    for char, name, extra in math_entries(args.math_class, unicode_names(args.unicode_data)):
        emit("math", char, name, extra)

    print(", ".join(f"{n} {kind}" for kind, n in counts.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
