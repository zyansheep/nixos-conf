#!/usr/bin/env python3
"""Build the dictionary database the Vicinae Japanese dictionary reads.

    build_db.py --jmdict JMDICT.json --kanjidic KANJIDIC.json --kradfile KRAD.json
                --kanjivg KANJIVG.zip --jpdb JPDB.zip --accents accents.txt
                --jlpt N5.csv N4.csv N3.csv N2.csv N1.csv --out jisho.sqlite3

Sources (all under open licences; see README.md):
- JMdict with Tatoeba examples (jmdict-simplified's JSON), the words.
- KANJIDIC2 and KRADFILE (jmdict-simplified's JSON), the kanji and their parts.
- KanjiVG, stroke order: recoloured here, first stroke to last along a ramp.
- JPDB's frequency ranks (Yomitan format), to rank words as people meet them.
- Jonathan Waller's JLPT lists (open-anki-jlpt-decks), the N5-N1 tags.
- kanjium's accents.txt, the pitch accents.

Japanese is matched on `fold`ed text (NFKC, katakana as hiragana) and English
on `english_key`s; src/lib/text.ts folds queries the same way, so the two
must change together.
"""
import argparse
import csv
import json
import re
import sqlite3
import unicodedata
import zipfile
from pathlib import Path

SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- One row per JMdict entry; `data` is the compact JSON src/lib/dict.ts reads.
CREATE TABLE entry(id INTEGER PRIMARY KEY, head TEXT NOT NULL, reading TEXT NOT NULL,
                   freq INTEGER, common INTEGER NOT NULL, jlpt INTEGER NOT NULL, data TEXT NOT NULL);
-- Every written form and reading, folded; `form` is its place in the entry (0 = the usual one).
CREATE TABLE term(term TEXT NOT NULL, id INTEGER NOT NULL, form INTEGER NOT NULL);
-- Every gloss's english_key, for exact English matches.
CREATE TABLE gkey(key TEXT NOT NULL, id INTEGER NOT NULL, sense INTEGER NOT NULL, pos INTEGER NOT NULL);
-- The same glosses for word matches ("eating" finds "to eat"), contentless: the rowid
-- is the gloss's (id, sense, pos) packed by gloss_rowid.
CREATE VIRTUAL TABLE gloss USING fts5(text, content='', columnsize=0, detail='none', tokenize='porter unicode61');
CREATE TABLE kanji(c TEXT PRIMARY KEY, freq INTEGER, data TEXT NOT NULL);
CREATE TABLE kkey(key TEXT NOT NULL, c TEXT NOT NULL);
-- Entries written with each kanji, for "words with 食".
CREATE TABLE kword(c TEXT NOT NULL, id INTEGER NOT NULL, freq INTEGER NOT NULL);
'''
INDEXES = '''
CREATE INDEX term_term ON term(term);
CREATE INDEX gkey_key ON gkey(key);
CREATE INDEX kkey_key ON kkey(key);
CREATE INDEX kword_c ON kword(c, freq);
'''
UNRANKED = 1 << 20  # Sorts after every ranked word.


def fold(text):
    """NFKC, lower case, katakana as hiragana: ネコ, ﾈｺ and ねこ all match."""
    text = unicodedata.normalize('NFKC', text).lower()
    return ''.join(chr(ord(c) - 0x60) if 'ァ' <= c <= 'ヶ' or c in 'ヽヾ' else c for c in text)


def english_key(text):
    """A gloss or query as matched exactly: no parentheses, case or leading
    "to"/"a"/"the" ("to eat (something)" -> "eat")."""
    text, before = text.lower(), None
    while before != text:  # Innermost first: "dog (Canis (lupus) familiaris)".
        text, before = re.sub(r'\([^()]*\)', ' ', text), text
    text = re.sub(r'\s+', ' ', text).strip(' .,;:!?"\'')
    for prefix in ('to ', 'a ', 'an ', 'the '):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def gloss_rowid(id_, sense, pos):
    """The gloss index's rowid for an entry's sense and gloss (dict.ts unpacks it)."""
    assert sense < 256 and pos < 256
    return id_ << 16 | sense << 8 | pos


def xref(ref):
    """A cross reference (["丸", "まる・1", 1], ["同上"]) as [written, reading or None]."""
    texts = [x for x in ref if isinstance(x, str)]
    return [texts[0], re.sub(r'・\d+$', '', texts[1]) if len(texts) > 1 else None]


def is_kanji(c):
    return '㐀' <= c <= '鿿' or '豈' <= c <= '﫿' or '\U00020000' <= c <= '\U0003134f' or c == '々'


def jpdb_ranks(path):
    """{(written, reading or None): best rank}; the ㋕ ranks (the word written
    in kana) count for the same word."""
    ranks = {}
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.startswith('term_meta_bank'):
                continue
            for term, kind, value in json.loads(archive.read(name)):
                if kind != 'freq':
                    continue
                reading = value.get('reading') if isinstance(value, dict) else None
                rank = value.get('frequency', value) if isinstance(value, dict) else value
                rank = rank.get('value') if isinstance(rank, dict) else rank
                if isinstance(rank, int):
                    key = (term, reading)
                    ranks[key] = min(rank, ranks.get(key, rank))
    return ranks


def pitch_accents(path):
    """{(written, reading): [downstep mora, ...]} from kanjium's accents.txt."""
    accents = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        parts = line.split('\t')
        if len(parts) != 3:
            continue
        word, reading, marks = parts
        downsteps = []
        for mark in marks.split(','):
            mark = re.sub(r'\([^)]*\)', '', mark).strip()
            if mark.isdigit() and int(mark) not in downsteps:
                downsteps.append(int(mark))
        if downsteps:
            accents[(word, reading or word)] = downsteps
    return accents


def jlpt_levels(paths):
    """{(written or reading, reading): level} from the N5..N1 lists (the easiest wins)."""
    levels = {}
    for path in paths:
        level = int(re.search(r'n([1-5])\.csv$', Path(path).name.lower()).group(1))  # (store paths start with a hash)
        with open(path, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                for written in row['expression'].split(';'):
                    for reading in row['reading'].split(';'):
                        key = (written.strip(), reading.strip() or written.strip())
                        levels[key] = max(level, levels.get(key, 0))
    return levels


def stroke_svg(source):
    """KanjiVG's SVG redrawn small: each stroke and its number coloured along
    a ramp from the first stroke to the last, for a dark background."""
    paths = re.findall(r'<path [^>]*\bd="([^"]+)"', source)
    numbers = re.findall(r'<text transform="matrix\(1 0 0 1 ([\d.]+) ([\d.]+)\)">(\d+)</text>', source)
    if not paths:
        return None
    count = len(paths)

    def colour(i):
        # Teal to amber through a light sky blue: distinct on dark and light themes.
        stops = ((0x4f, 0xd1, 0xc5), (0x8a, 0xb4, 0xf8), (0xf6, 0xb2, 0x6b))
        t = i / max(1, count - 1) * (len(stops) - 1)
        a, b = stops[min(int(t), len(stops) - 2)], stops[min(int(t), len(stops) - 2) + 1]
        f = t - min(int(t), len(stops) - 2)
        return '#%02x%02x%02x' % tuple(round(x + (y - x) * f) for x, y in zip(a, b))

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="109" height="109" viewBox="0 0 109 109">',
           '<g fill="none" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">']
    out += [f'<path stroke="{colour(i)}" d="{d}"/>' for i, d in enumerate(paths)]
    out.append('</g><g font-size="8" font-family="sans-serif">')
    for x, y, n in numbers:
        out.append(f'<text x="{x}" y="{y}" fill="{colour(int(n) - 1)}">{n}</text>')
    out.append('</g></svg>')
    return ''.join(out)


def entries(words, ranks, accents, levels):
    for w in words:
        kanji = [(k['text'], int(k['common']), k['tags']) for k in w['kanji']]
        kana = [(r['text'], int(r['common']), r['tags'], r['appliesToKanji']) for r in w['kana']]
        senses = []
        for s in w['sense']:
            sense = {'p': s['partOfSpeech'], 'g': [g['text'] for g in s['gloss']]}
            for key, field in (('m', 'misc'), ('f', 'field'), ('d', 'dialect'), ('i', 'info')):
                if s[field]:
                    sense[key] = s[field]
            if s['appliesToKanji'] != ['*']:
                sense['ak'] = s['appliesToKanji']
            if s['appliesToKana'] != ['*']:
                sense['ar'] = s['appliesToKana']
            if s['related']:
                sense['rel'] = [xref(r) for r in s['related']]
            if s['antonym']:
                sense['ant'] = [xref(r) for r in s['antonym']]
            if s['languageSource']:
                sense['ls'] = [[l['lang'], l['text'] or '', int(l['wasei'])] for l in s['languageSource']]
            if any(g['type'] for g in s['gloss']):
                sense['gt'] = [g['type'] or '' for g in s['gloss']]
            examples = []
            for e in s.get('examples') or []:
                text = {x['lang']: x['text'] for x in e['sentences']}
                if 'jpn' in text and 'eng' in text:
                    examples.append([text['jpn'], text['eng'], e['text']])
            if examples:
                sense['x'] = examples
            senses.append(sense)

        def applies(reading, written):
            return reading[3] == ['*'] or written in reading[3]

        shown_kanji = [k for k in kanji if 'sK' not in k[2]]
        shown_kana = [r for r in kana if 'sk' not in r[2]] or kana
        usually_kana = 'uk' in (senses[0].get('m') or [])
        if shown_kanji and not usually_kana:
            head = shown_kanji[0][0]
            reading = next((r[0] for r in shown_kana if applies(r, head)), shown_kana[0][0])
        else:
            head = reading = shown_kana[0][0]

        pairs = [(k[0], r[0]) for k in kanji for r in kana if applies(r, k[0])]
        found = [ranks[p] for p in pairs if p in ranks]
        if not kanji or usually_kana and not found:
            found += [ranks[(r[0], None)] for r in kana if (r[0], None) in ranks]
        freq = min(found) if found else None

        pitch = {}
        for written, read in pairs + [(r[0], r[0]) for r in kana]:
            if read not in pitch and (written, read) in accents:
                pitch[read] = accents[(written, read)]
        jlpt = max([levels.get(p, 0) for p in pairs + [(r[0], r[0]) for r in kana]] or [0])
        data = {'k': kanji, 'r': kana, 's': senses}
        if pitch:
            data['pa'] = pitch
        common = int(any(k[1] for k in kanji) or any(r[1] for r in kana))
        yield int(w['id']), head, reading, freq, common, jlpt, data


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for name in ('jmdict', 'kanjidic', 'kradfile', 'kanjivg', 'jpdb', 'accents', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--jlpt', nargs='+', required=True)
    args = parser.parse_args()

    out = Path(args.out)
    out.unlink(missing_ok=True)
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)
    jmdict = json.loads(Path(args.jmdict).read_text(encoding='utf-8'))
    ranks, accents, levels = jpdb_ranks(args.jpdb), pitch_accents(args.accents), jlpt_levels(args.jlpt)

    words, kanji_words = 0, {}
    for id_, head, reading, freq, common, jlpt, data in entries(jmdict['words'], ranks, accents, levels):
        words += 1
        db.execute('INSERT INTO entry VALUES (?,?,?,?,?,?,?)',
                   (id_, head, reading, freq, common, jlpt, json.dumps(data, ensure_ascii=False, separators=(',', ':'))))
        forms = [head] + [k[0] for k in data['k']] + [r[0] for r in data['r']]
        seen = set()
        for form, text in enumerate(forms):
            if fold(text) not in seen:
                seen.add(fold(text))
                db.execute('INSERT INTO term VALUES (?,?,?)', (fold(text), id_, form))
        for s, sense in enumerate(data['s']):
            for g, gloss in enumerate(sense['g']):
                db.execute('INSERT INTO gkey VALUES (?,?,?,?)', (english_key(gloss), id_, s, g))
                db.execute('INSERT INTO gloss(rowid, text) VALUES (?,?)', (gloss_rowid(id_, s, g), gloss.lower()))
        rank = freq if freq is not None else UNRANKED + (0 if common else 1)
        for c in {c for k in data['k'] if 'sK' not in k[2] for c in k[0] if is_kanji(c)}:
            kanji_words.setdefault(c, []).append((rank, id_))

    for c, found in kanji_words.items():
        db.executemany('INSERT INTO kword VALUES (?,?,?)', [(c, id_, rank) for rank, id_ in sorted(found)[:60]])

    parts = json.loads(Path(args.kradfile).read_text(encoding='utf-8'))['kanji']
    with zipfile.ZipFile(args.kanjivg) as archive:
        strokes = {}
        for name in archive.namelist():
            m = re.fullmatch(r'kanji/([0-9a-f]{5})\.svg', name)
            if m:
                strokes[chr(int(m.group(1), 16))] = stroke_svg(archive.read(name).decode('utf-8'))
    kanjidic = json.loads(Path(args.kanjidic).read_text(encoding='utf-8'))
    for ch in kanjidic['characters']:
        c, misc = ch['literal'], ch['misc']
        groups = (ch['readingMeaning'] or {}).get('groups') or []
        readings = [r for g in groups for r in g['readings']]
        meanings = [m['value'] for g in groups for m in g['meanings'] if m['lang'] == 'en']
        radical = next((r['value'] for r in ch['radicals'] if r['type'] == 'classical'), None)
        data = {'m': meanings,
                'on': [r['value'] for r in readings if r['type'] == 'ja_on'],
                'kun': [r['value'] for r in readings if r['type'] == 'ja_kun'],
                'na': (ch['readingMeaning'] or {}).get('nanori') or [],
                'sc': misc['strokeCounts'][0] if misc['strokeCounts'] else None,
                'gr': misc['grade'], 'jl': misc['jlptLevel'], 'fr': misc['frequency'],
                'rad': radical, 'parts': [p for p in parts.get(c, []) if p != c]}
        if strokes.get(c):
            data['svg'] = strokes[c]
        db.execute('INSERT INTO kanji VALUES (?,?,?)',
                   (c, misc['frequency'], json.dumps(data, ensure_ascii=False, separators=(',', ':'))))
        for key in {english_key(m) for m in meanings}:
            db.execute('INSERT INTO kkey VALUES (?,?)', (key, c))

    meta = {'jmdict': jmdict['dictDate'], 'kanjidic': kanjidic['dictDate'], 'words': str(words),
            'tags': json.dumps(jmdict['tags'], ensure_ascii=False)}
    db.executemany('INSERT INTO meta VALUES (?,?)', meta.items())
    db.executescript(INDEXES)
    db.execute("INSERT INTO gloss(gloss) VALUES ('optimize')")
    db.commit()
    db.execute('VACUUM')
    db.close()


if __name__ == '__main__':
    main()
