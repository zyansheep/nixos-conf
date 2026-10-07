"""Build the emoji-picker list: one `value<TAB>display` line per entry.

The value is what gets inserted; the display line is what Vicinae's dmenu
shows and fuzzy-matches, so it carries every searchable name and keyword.

  emoji    Unicode emoji-test.txt (fully-qualified only, no bare components),
           names from the file, keywords from CLDR's English annotations.
  kaomoji  `--kaomoji FORMAT=PATH` collections, parsed by the KAOMOJI
           readers below, filtered, deduplicated and with their tags merged.
           There's no usage data, so popularity is how many collections carry
           a face; the --kaomoji-limit most popular are kept, most popular
           first, ties going to the earlier (more curated) collection.
  math     Unicode MathClassEx (what rofimoji's `math` set was built from),
           with its entity and TeX names (rArr, /wedge, /land) as keywords.
"""

import argparse
import csv
import gzip
import html
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

# Vicinae's dmenu treats an entry starting with "/" as a file path and shows
# only its last component, which mangles kaomoji like /ᐠ｡ꞈ｡ᐟ\ — an invisible
# word joiner in front of the display text sidesteps that.
WORD_JOINER = "⁠"

# A control character anywhere in the dmenu input breaks Vicinae's IPC
# decoding of the rest of the request (the query, the options), so nothing
# from these categories reaches the list. Some collections also carry
# mojibake, which shows up as C1 controls.
FORBIDDEN = {"Cc", "Cs", "Co", "Zl", "Zp"}


def clean(text):
    text = "".join(c for c in text if unicodedata.category(c) not in FORBIDDEN)
    return " ".join(text.split())


def cldr_keywords(paths):
    keywords = {}
    for path in paths:
        for node in ET.parse(path).getroot().iter("annotation"):
            if node.get("type") == "tts" or not node.text:
                continue
            words = [clean(w) for w in node.text.split("|")]
            keywords.setdefault(node.get("cp").replace("️", ""), words)
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
        extra = [w for w in keywords.get(char.replace("️", ""), [])
                 if w.lower() != name.lower() and not set(w.lower().split()) <= name_words]
        yield char, name, extra


# --- kaomoji ---------------------------------------------------------------
# Each reader yields (kaomoji, [tags]) from one collection's file layout.

KAOMOJI = {}


def reader(name):
    def register(fn):
        KAOMOJI[name] = fn
        return fn
    return register


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def by_category(data, unescape=False):
    for category, items in data.items():
        for k in items:
            yield (html.unescape(k) if unescape else k), [category]


@reader("w33ble")  # rofimoji's kaomoji set
def _(path):
    for e in load_json(path)["emoticons"]:
        yield e["string"], e.get("tags", [])


@reader("all-in-one-clipboard")
def _(path):
    for group in load_json(path)["data"]:
        for category in group["categories"]:
            for e in category["emoticons"]:
                yield e["kaomoji"], [category["name"], *e.get("keywords", [])]


@reader("by-category")  # {category: [kaomoji, ...]}
def _(path):
    yield from by_category(load_json(path))


@reader("kaomojiya")  # by-category, with HTML entities
def _(path):
    yield from by_category(load_json(path), unescape=True)


@reader("codingstark")  # {group: {category: [kaomoji, ...]}}
def _(path):
    for categories in load_json(path).values():
        yield from by_category(categories)


@reader("vsedov")  # directory of kaomoji<TAB>title<TAB>category files
def _(path):
    # splatmoji.tsv is another copy of rofimoji's set, already counted.
    for tsv in sorted(p for p in Path(path).glob("*.tsv") if p.name != "splatmoji.tsv"):
        for line in tsv.read_text(encoding="utf-8").splitlines():
            k, *tags = line.split("\t")
            yield k, tags


@reader("asciilib")
def _(path):
    for e in load_json(path).values():
        yield e["entry"], [e.get("name", ""), *e.get("keywords", [])]


@reader("elisa-aleman")
def _(path):
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            yield row["Kaomoji"], [row["Title"], row["Emotion"], row["Character"]]


@reader("fontvibe")
def _(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            yield e["text"], [*e.get("keywords", {}).get("en", []), e.get("category", "")]


@reader("kaomojikan")
def _(path):
    for e in load_json(path):
        yield e["text"], [*e.get("tags", []), *e.get("reading", [])]


@reader("azookey")  # reading<TAB>kaomoji<TAB>...
def _(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) > 1:
            yield fields[1], [fields[0]]


@reader("kaomoji-json")
def _(path):
    for e in load_json(path):
        yield e["face"], [e.get("annotation", "")]


@reader("ekohrt")
def _(path):
    for k, e in load_json(path).items():
        yield k, [*e.get("original_tags", []), *e.get("new_tags", [])]


# Three kana/kanji/hangul in a row is words, not a face. ・ ー ヽ ヾ and the
# half-width ｰ ﾞ ﾟ are left out: they're eyes, mouths and arms in kaomoji.
DIALOGUE = re.compile("[ぁ-ゖァ-ヺ一-鿿ｦ-ｯｱ-ﾝ가-힯]{3,}")
PICTOGRAPHS = re.compile("^[\U0001F000-\U0001FAFF☀-➿️‍\\s]+$")
WESTERN = re.compile(r"^(>?[:;=8xXB%][-o^'c]?[)(DPpOo0/\\|\]\[*3>$@#&]{1,3}|[)(D\]\[/\\|]{1,2}[-o^']?[:;=8])$")
ENTITY = re.compile(r"&(amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);")
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"))
NOISE_TAGS = {"uncategorized", "unassigned", "special", "other", "others", "misc",
              "kaomoji", "kaomojis", "emoticon", "emoticons"}
MAX_TAGS = 8


def usable_kaomoji(k):
    """One line, face-sized, no Japanese sentence attached, not just emoji
    or a :) that's quicker to type."""
    return (3 <= len(k) <= 40
            and not any(unicodedata.category(c) in FORBIDDEN | {"Cn"} for c in k)
            and not PICTOGRAPHS.match(k) and not DIALOGUE.search(k)
            and not ENTITY.search(k) and not WESTERN.match(k))


def normalise_tag(tag):
    tag = unicodedata.normalize("NFKC", tag).translate(ZERO_WIDTH)  # ｓａｄ -> sad
    tag = clean(tag.replace("_", " ")).lower()
    return re.sub(r" \d+$", "", tag)  # ekohrt's table_flip_2


def kaomoji_entries(sources, limit):
    merged = {}  # spacing-insensitive key -> (kaomoji, tags, collections carrying it)
    for spec in sources:
        fmt, _, path = spec.partition("=")
        for k, tags in KAOMOJI[fmt](path):
            k = unicodedata.normalize("NFC", k).strip().strip("​⁠﻿").strip()
            if not usable_kaomoji(k):
                continue
            key = " ".join(k.translate(ZERO_WIDTH).split())
            _, known, carriers = merged.setdefault(key, (k, [], set()))
            carriers.add(spec)
            parts = (part for tag in tags for part in tag.split(","))  # some are "a, b, c"
            for tag in map(normalise_tag, parts):
                if tag and tag not in NOISE_TAGS and tag not in known and tag != key:
                    known.append(tag)
    # sorted() is stable, so equal counts keep first-seen (collection) order.
    ranked = sorted(merged.values(), key=lambda entry: -len(entry[2]))
    for k, tags, _ in ranked[:limit]:
        yield k, tags[:MAX_TAGS]


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
    ap.add_argument("--kaomoji", action="append", default=[], metavar="FORMAT=PATH")
    ap.add_argument("--kaomoji-limit", type=int)
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

    for char, name, extra in emoji_entries(args.emoji_test, cldr_keywords(args.cldr)):
        emit("emoji", char, name, extra)
    for value, tags in kaomoji_entries(args.kaomoji, args.kaomoji_limit):
        emit("kaomoji", value, ", ".join(tags), [])
    for char, name, extra in math_entries(args.math_class, unicode_names(args.unicode_data)):
        emit("math", char, name, extra)

    print(", ".join(f"{n} {kind}" for kind, n in counts.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
