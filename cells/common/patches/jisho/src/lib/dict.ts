// Lookups in the database build_db.py makes: Japanese (as typed, inflected,
// or as a prefix), English (exact gloss, then word matches), romaji, kanji,
// and sentences split into the words they're made of.
import { DatabaseSync, type StatementSync } from "node:sqlite";
import { classesOf, type Deinflection, deinflect, describe } from "./inflect.ts";
import { englishKey, fold, hasJapanese, isKana, isKanji, kanjiOf, romajiToKana } from "./text.ts";

export type Ref = [written: string, reading: string | null];
export type Sense = {
	p: string[]; // Parts of speech.
	g: string[]; // Glosses.
	m?: string[]; // Misc tags (uk, col, hon, ...).
	f?: string[]; // Fields (comp, med, ...).
	d?: string[]; // Dialects.
	i?: string[]; // Notes.
	ak?: string[]; // The written forms it's limited to.
	ar?: string[]; // The readings it's limited to.
	rel?: Ref[];
	ant?: Ref[];
	ls?: [lang: string, word: string, wasei: number][];
	gt?: string[]; // Gloss types (literal, figurative, explanation).
	x?: [japanese: string, english: string, form: string][]; // Tatoeba examples.
};
export type Entry = {
	id: number;
	head: string;
	reading: string;
	freq: number | null; // JPDB rank.
	common: boolean;
	jlpt: number; // 5 = N5 ... 1 = N1, 0 = none.
	k: [text: string, common: number, tags: string[]][];
	r: [text: string, common: number, tags: string[], appliesTo: string[]][];
	s: Sense[];
	pa?: Record<string, number[]>; // Pitch accent downsteps by reading.
};
export type Kanji = {
	c: string;
	m: string[];
	on: string[];
	kun: string[];
	na: string[];
	sc: number | null;
	gr: number | null;
	jl: number | null; // Pre-2010 JLPT level, 4 easiest.
	fr: number | null; // Newspaper frequency rank of the 2500 most used.
	rad: number | null; // Kangxi radical number.
	parts: string[];
	svg?: string;
};
export type Hit = {
	entry: Entry;
	score: number; // Lower is better.
	/** How the query was inflected from the entry ("polite past"), if it was. */
	inflection?: string;
	/** The text it matched: the query, or the romaji's kana. */
	matched: string;
};
export type Segment = { text: string; hits: Hit[] };

const UNRANKED = 1 << 20;
const PREFIX_END = "\u{10ffff}";

/** How common a word is, as a score penalty: rank 100 -> 16, 10 000 -> 32. */
export function rarity(entry: Entry): number {
	const base = entry.freq ? 8 * Math.log10(Math.max(entry.freq, 10)) : entry.common ? 34 : 44;
	return base - (entry.jlpt ? 3 : 0);
}

const IRREGULAR_FORM = /^(rK|sK|oK|iK|ik|ok|rk|sk)$/;

/** Matching an entry by one of its other forms: a little worse, and an odd one (rare, outdated) much worse. */
function formPenalty(entry: Entry, form: number): number {
	if (!form) return 0;
	const tags = form <= entry.k.length ? entry.k[form - 1]?.[2] : entry.r[form - 1 - entry.k.length]?.[2];
	return tags?.some((t) => IRREGULAR_FORM.test(t)) ? 14 : 4;
}

/** An inflected match ranks below an exact one, the more so the longer its chain. */
function inflectionPenalty(d: Deinflection): number {
	return 14 + 3 * d.reasons.length + (d.reasons.includes("masu stem") ? 15 : 0);
}

export class Dictionary {
	private db: DatabaseSync;
	private cache = new Map<number, Entry>();
	private statements = new Map<string, StatementSync>();
	tags: Record<string, string>;
	meta: Record<string, string>;

	constructor(path: string) {
		this.db = new DatabaseSync(path, { readOnly: true });
		this.meta = Object.fromEntries(
			(this.query("SELECT key, value FROM meta") as { key: string; value: string }[]).map((r) => [r.key, r.value]),
		);
		this.tags = JSON.parse(this.meta.tags ?? "{}");
	}

	close() {
		this.db.close();
	}

	private statement(sql: string): StatementSync {
		let s = this.statements.get(sql);
		if (!s) {
			s = this.db.prepare(sql);
			this.statements.set(sql, s);
		}
		return s;
	}

	private query(sql: string, ...params: (string | number)[]): Record<string, unknown>[] {
		return this.statement(sql).all(...params) as Record<string, unknown>[];
	}

	entry(id: number): Entry | undefined {
		let e = this.cache.get(id);
		if (!e) {
			const row = this.statement("SELECT * FROM entry WHERE id = ?").get(id) as
				| { id: number; head: string; reading: string; freq: number | null; common: number; jlpt: number; data: string }
				| undefined;
			if (!row) return undefined;
			e = { ...JSON.parse(row.data), id: row.id, head: row.head, reading: row.reading, freq: row.freq, common: !!row.common, jlpt: row.jlpt } as Entry;
			if (this.cache.size > 2000) this.cache.clear();
			this.cache.set(id, e);
		}
		return e;
	}

	/** Entries with this exact (folded) form: [id, its place among the entry's forms]. */
	private exact(term: string): { id: number; form: number }[] {
		return this.query("SELECT id, form FROM term WHERE term = ?", term) as { id: number; form: number }[];
	}

	private known(term: string): boolean {
		return !!this.statement("SELECT 1 FROM term WHERE term = ? LIMIT 1").get(term);
	}

	/** Entries the deinflection could be, as [hit score, entry]. */
	private inflected(d: Deinflection): { entry: Entry; form: number }[] {
		const out: { entry: Entry; form: number }[] = [];
		const types = d.types ?? [];
		const candidates = [...this.exact(d.text).map((row) => ({ ...row, noun: false }))];
		// 勉強した: 勉強する isn't a word of its own; 勉強 is a noun taking する.
		if (d.text.endsWith("する") && types.includes("vs-i") && d.text.length > 2) {
			candidates.push(...this.exact(d.text.slice(0, -2)).map((row) => ({ ...row, noun: true })));
		}
		for (const { id, form, noun } of candidates) {
			const entry = this.entry(id);
			if (!entry) continue;
			const pos = entry.s.flatMap((s) => s.p);
			const fits = noun ? pos.includes("vs") : classesOf(pos).some((c) => types.includes(c));
			if (fits) out.push({ entry, form });
		}
		return out;
	}

	/** Japanese as typed: exact forms, then inflections, then words starting so. */
	japanese(query: string, limit = 40): Hit[] {
		const q = fold(query.trim());
		if (!q) return [];
		const hits = new Map<number, Hit>();
		const add = (entry: Entry, score: number, inflection?: string) => {
			const total = score + rarity(entry);
			const old = hits.get(entry.id);
			if (!old || total < old.score) hits.set(entry.id, { entry, score: total, inflection, matched: query });
		};
		for (const { id, form } of this.exact(q)) {
			const entry = this.entry(id);
			if (entry) add(entry, formPenalty(entry, form));
		}
		if (isKana(q.at(-1) as string)) {
			for (const d of deinflect(q).slice(1)) {
				for (const { entry, form } of this.inflected(d)) {
					add(entry, inflectionPenalty(d) + formPenalty(entry, form), describe(d.reasons));
				}
			}
		}
		const prefix = this.query(
			`SELECT t.id, min(length(t.term)) len FROM term t JOIN entry e ON e.id = t.id
			 WHERE t.term > ? AND t.term < ? GROUP BY t.id
			 ORDER BY coalesce(e.freq, ${UNRANKED}) + (1 - e.common) LIMIT 120`,
			q,
			q + PREFIX_END,
		) as { id: number; len: number }[];
		for (const { id, len } of prefix) {
			const entry = this.entry(id);
			if (entry) add(entry, 26 + Math.min(12, 2 * (len - q.length)));
		}
		return [...hits.values()].sort((a, b) => a.score - b.score).slice(0, limit);
	}

	/** English: glosses that are the query ("eat" = "to eat"), start with it, or have its words. */
	english(query: string, limit = 40): Hit[] {
		const key = englishKey(query);
		if (!key) return [];
		const best = new Map<number, number>();
		const consider = (id: number, score: number) => {
			if (score < (best.get(id) ?? Number.POSITIVE_INFINITY)) best.set(id, score);
		};
		for (const { id, sense, pos } of this.query("SELECT id, sense, pos FROM gkey WHERE key = ?", key) as {
			id: number;
			sense: number;
			pos: number;
		}[]) {
			consider(id, Math.min(24, 8 * sense) + Math.min(12, 4 * pos));
		}
		const words = key.match(/[\p{L}\p{N}]+/gu) ?? [];
		if (words.length) {
			const match = words.map((w) => `"${w}"`).join(" ");
			const rows = this.query("SELECT rowid FROM gloss WHERE gloss MATCH ? LIMIT 6000", match) as { rowid: number }[];
			for (const { rowid } of rows) {
				const id = Math.floor(rowid / 65536);
				const sense = Math.floor(rowid / 256) % 256;
				const pos = rowid % 256;
				consider(id, 34 + Math.min(24, 8 * sense) + Math.min(12, 4 * pos));
			}
		}
		// Rank by match and rarity, then look closer at the best: a gloss that
		// starts with the query ("eat out") beats one that merely has its words.
		const ranked = [...best.entries()]
			.map(([id, score]) => {
				const row = this.statement("SELECT freq, common, jlpt FROM entry WHERE id = ?").get(id) as {
					freq: number | null;
					common: number;
					jlpt: number;
				};
				return { id, score: score + rarity({ ...row, common: !!row.common } as Entry) };
			})
			.sort((a, b) => a.score - b.score)
			.slice(0, limit * 3);
		const hits: Hit[] = [];
		for (const { id, score } of ranked) {
			const entry = this.entry(id);
			if (!entry) continue;
			let bonus = 0;
			if (score - rarity(entry) >= 34) {
				const glosses = entry.s.flatMap((s, i) => s.g.map((g) => ({ g: englishKey(g), i })));
				const starts = glosses.find(({ g }) => g.startsWith(`${key} `) || g.startsWith(`${key},`));
				if (starts) bonus = -14 + Math.min(10, 4 * starts.i);
				else if (glosses.some(({ g }) => new RegExp(`\\b${escapeRegExp(key)}\\b`).test(g))) bonus = -8;
			}
			hits.push({ entry, score: score + bonus, matched: query });
		}
		return hits.sort((a, b) => a.score - b.score).slice(0, limit);
	}

	/** Anything: Japanese, English, or romaji (both its English and its kana are tried). */
	search(query: string, limit = 40): Hit[] {
		const q = query.trim();
		if (!q) return [];
		if (hasJapanese(q)) return this.japanese(q, limit);
		const hits = new Map<number, Hit>();
		for (const hit of this.english(q, limit)) hits.set(hit.entry.id, hit);
		const kana = romajiToKana(q);
		if (kana) {
			for (const hit of this.japanese(kana, limit)) {
				const old = hits.get(hit.entry.id);
				const score = hit.score + 4;
				if (!old || score < old.score) hits.set(hit.entry.id, { ...hit, score, matched: kana });
			}
		}
		return [...hits.values()].sort((a, b) => a.score - b.score).slice(0, limit);
	}

	/** The words a Japanese sentence is made of, longest match first at each place. */
	segment(sentence: string): Segment[] {
		const text = sentence.normalize("NFKC");
		const chars = [...text];
		const out: Segment[] = [];
		let i = 0;
		while (i < chars.length) {
			const c = chars[i];
			if (!isKana(c) && !isKanji(c)) {
				const last = out.at(-1);
				if (last && !last.hits.length && !hasJapanese(last.text)) last.text += c;
				else out.push({ text: c, hits: [] });
				i++;
				continue;
			}
			let found: Segment | null = null;
			for (let len = Math.min(16, chars.length - i); len >= 1 && !found; len--) {
				const sub = chars.slice(i, i + len).join("");
				if (len > 1 && !chars.slice(i, i + len).every((x) => isKana(x) || isKanji(x))) continue;
				const hits = this.word(sub);
				if (hits.length) found = { text: sub, hits };
			}
			if (!found) found = { text: c, hits: [] };
			out.push(found);
			i += [...found.text].length;
		}
		return out;
	}

	/** Entries this exact word is, as written or inflected (no prefixes). */
	word(text: string): Hit[] {
		const q = fold(text);
		const hits = new Map<number, Hit>();
		const add = (entry: Entry, score: number, inflection?: string) => {
			const total = score + rarity(entry);
			const old = hits.get(entry.id);
			if (!old || total < old.score) hits.set(entry.id, { entry, score: total, inflection, matched: text });
		};
		for (const { id, form } of this.exact(q)) {
			const entry = this.entry(id);
			if (entry) add(entry, formPenalty(entry, form));
		}
		if (isKana(q.at(-1) as string)) {
			for (const d of deinflect(q).slice(1)) {
				if (!this.known(d.text) && !(d.text.endsWith("する") && this.known(d.text.slice(0, -2)))) continue;
				for (const { entry, form } of this.inflected(d)) add(entry, inflectionPenalty(d) + formPenalty(entry, form), describe(d.reasons));
			}
		}
		return [...hits.values()].sort((a, b) => a.score - b.score);
	}

	kanji(c: string): Kanji | undefined {
		const row = this.statement("SELECT data FROM kanji WHERE c = ?").get(c) as { data: string } | undefined;
		return row ? { c, ...JSON.parse(row.data) } : undefined;
	}

	/** The kanji in the text, in order, that the dictionary has. */
	kanjiIn(text: string): Kanji[] {
		return kanjiOf(text)
			.map((c) => this.kanji(c))
			.filter((k): k is Kanji => !!k);
	}

	/** Kanji meaning exactly this ("water" -> 水), most used first. */
	kanjiMeaning(query: string, limit = 4): Kanji[] {
		const rows = this.query(
			"SELECT k.c FROM kkey JOIN kanji k ON k.c = kkey.c WHERE kkey.key = ? ORDER BY coalesce(k.freq, 9999) LIMIT ?",
			englishKey(query),
			limit,
		) as { c: string }[];
		return rows.map((r) => this.kanji(r.c)).filter((k): k is Kanji => !!k);
	}

	/** Words written with the kanji, most common first. */
	wordsWith(c: string, limit = 8): Entry[] {
		const rows = this.query("SELECT id FROM kword WHERE c = ? ORDER BY freq LIMIT ?", c, limit) as { id: number }[];
		return rows.map((r) => this.entry(r.id)).filter((e): e is Entry => !!e);
	}
}

function escapeRegExp(s: string): string {
	return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
