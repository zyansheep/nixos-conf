// The detail pages: markdown with inline HTML, which Vicinae renders as Qt
// rich text (spans with colours, sizes and overlines; no ruby, no CSS classes).
import type { Dictionary, Entry, Hit, Kanji, Segment, Sense } from "./dict.ts";
import { conjugationTable, type ConjugationRow } from "./inflect.ts";
import { fold, isKanji, kanaToRomaji, morae } from "./text.ts";

export const MUTED = "#8d929a";
const ACCENT = "#8ab4f8";

const POS: Record<string, string> = {
	n: "Noun", "n-adv": "Adverbial noun", "n-t": "Temporal noun", "n-suf": "Noun, used as a suffix",
	"n-pref": "Noun, used as a prefix", "n-pr": "Proper noun", pn: "Pronoun", "adj-i": "I-adjective",
	"adj-ix": "I-adjective (よい/いい)", "adj-na": "Na-adjective", "adj-no": "No-adjective",
	"adj-pn": "Pre-noun adjectival", "adj-t": "Taru-adjective", "adj-f": "Noun or verb acting prenominally",
	adv: "Adverb", "adv-to": "Adverb taking と", aux: "Auxiliary", "aux-v": "Auxiliary verb",
	"aux-adj": "Auxiliary adjective", conj: "Conjunction", cop: "Copula", ctr: "Counter", exp: "Expression",
	int: "Interjection", num: "Numeric", pref: "Prefix", suf: "Suffix", prt: "Particle", unc: "Unclassified",
	v1: "Ichidan verb", "v1-s": "Ichidan verb (くれる class)", "v5k-s": "Godan verb (行く class)",
	"v5r-i": "Godan verb, irregular (ある)", v5aru: "Godan verb (〜ある class)", "v5u-s": "Godan verb (問う class)",
	vk: "Kuru verb", vs: "Takes する", "vs-i": "Suru verb", "vs-s": "Suru verb (special)", "vs-c": "Su verb (archaic)",
	vz: "Zuru verb", vi: "intransitive", vt: "transitive",
};
const MISC: Record<string, string> = {
	uk: "usually written in kana", col: "colloquial", hon: "honorific", hum: "humble", pol: "polite",
	sl: "slang", "net-sl": "internet slang", abbr: "abbreviation", arch: "archaic", obs: "obsolete",
	dated: "dated", rare: "rare", vulg: "vulgar", derog: "derogatory", fam: "familiar", fem: "feminine",
	male: "masculine", joc: "jocular", id: "idiom", "on-mim": "onomatopoeia", poet: "poetic",
	proverb: "proverb", yoji: "four-character idiom", euph: "euphemism", form: "formal",
	chn: "children's language", sens: "sensitive", work: "title of a work", char: "character",
};
const FORMS: Record<string, string> = {
	ateji: "ateji", iK: "irregular kanji", ik: "irregular kana", io: "irregular okurigana", oK: "outdated kanji",
	ok: "outdated kana", rK: "rarely used kanji", rk: "rarely used kana", sK: "search-only", sk: "search-only",
	gikun: "gikun reading",
};
const LANGUAGES: Record<string, string> = {
	eng: "English", ger: "German", fre: "French", por: "Portuguese", dut: "Dutch", ita: "Italian",
	spa: "Spanish", rus: "Russian", chi: "Chinese", kor: "Korean", lat: "Latin", gre: "Greek",
	ain: "Ainu", san: "Sanskrit", ara: "Arabic", tib: "Tibetan", mon: "Mongolian",
};

/** Text for a markdown paragraph that may hold inline HTML: no tags, no emphasis or strikethrough. */
export function escape(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/([\\`*_[\]~|])/g, "\\$1");
}

function muted(html: string): string {
	return `<span style="color:${MUTED}">${html}</span>`;
}

/** A small coloured label (Qt rich text has no backgrounds on spans worth using). */
function chip(text: string, colour: string): string {
	return `<span style="color:${colour};font-size:9pt"><b>${escape(text)}</b></span>`;
}

// The JLPT levels' colours, as the list's tags show them (N5 green ... N1 red).
export const JLPT_HEX: Record<number, string> = { 5: "#7fc8a0", 4: "#8ab4f8", 3: "#e8c46b", 2: "#f0a35e", 1: "#f28b82" };

export function posLabel(tag: string, tags: Record<string, string>): string {
	if (POS[tag]) return POS[tag];
	const godan = tag.match(/^v5(.)$/);
	if (godan) return `Godan verb (〜${{ u: "う", k: "く", g: "ぐ", s: "す", t: "つ", n: "ぬ", b: "ぶ", m: "む", r: "る" }[godan[1]] ?? godan[1]})`;
	return capitalize(tags[tag] ?? tag);
}

function tagLabel(tag: string, tags: Record<string, string>): string {
	return MISC[tag] ?? FORMS[tag] ?? tags[tag] ?? tag;
}

function capitalize(s: string): string {
	return s.charAt(0).toUpperCase() + s.slice(1);
}

/** The senses' parts of speech as Jisho shows them: "Ichidan verb, transitive". */
export function posLine(pos: string[], tags: Record<string, string>): string {
	const main = pos.filter((p) => p !== "vt" && p !== "vi").map((p) => posLabel(p, tags));
	const transitivity = pos.filter((p) => p === "vt" || p === "vi").map((p) => POS[p]);
	return [main.join(" · "), ...transitivity].filter(Boolean).join(", ");
}

/** The reading with its pitch: high morae overlined, ꜜ where it drops. */
export function pitchHtml(reading: string, downstep: number): string {
	const m = morae(reading);
	let html = "";
	let high = "";
	const flush = () => {
		if (high) html += `<span style="text-decoration:overline">${high}</span>`;
		high = "";
	};
	m.forEach((mora, i) => {
		const isHigh = downstep === 0 ? i > 0 : downstep === 1 ? i === 0 : i > 0 && i < downstep;
		if (isHigh) high += mora;
		else {
			flush();
			html += mora;
		}
		if (downstep > 0 && i === downstep - 1) {
			flush();
			html += "ꜜ";
		}
	});
	flush();
	return html;
}

const PITCH_NAMES = ["heiban", "atamadaka", "nakadaka", "odaka"];
function pitchName(reading: string, downstep: number): string {
	if (downstep === 0) return PITCH_NAMES[0];
	if (downstep === 1) return PITCH_NAMES[1];
	return downstep >= morae(reading).length ? PITCH_NAMES[3] : PITCH_NAMES[2];
}

/** The glosses of a sense, joined; explanations in italics. */
function glossText(sense: Sense): string {
	return sense.g
		.map((g, i) => (sense.gt?.[i] === "explanation" ? `<i>${escape(g)}</i>` : escape(g)))
		.join("; ");
}

/** A one-line summary for the list: the first two senses' first glosses. */
export function summary(entry: Entry): string {
	const parts: string[] = [];
	for (const sense of entry.s.slice(0, 3)) {
		parts.push(sense.g.slice(0, 2).join(", "));
		if (parts.join("; ").length > 50) break;
	}
	return parts.join("; ");
}

/** At most n characters, cut at a word: the word-by-word list stays one line a word. */
function clip(text: string, n: number): string {
	if (text.length <= n) return text;
	const cut = text.slice(0, n);
	return `${cut.slice(0, Math.max(cut.lastIndexOf(" "), n / 2)).replace(/[;,]$/, "")}…`;
}

/** The readings shown for the entry's main form (those that apply to it). */
export function readingsOf(entry: Entry): string[] {
	const out = entry.r
		.filter((r) => !r[2].includes("sk") && (r[3].includes("*") || r[3].includes(entry.head)))
		.map((r) => r[0]);
	return out.length ? out : [entry.reading];
}

function senseNotes(sense: Sense, tags: Record<string, string>): string[] {
	const notes: string[] = [];
	for (const tag of [...(sense.m ?? []), ...(sense.f ?? []), ...(sense.d ?? [])]) notes.push(tagLabel(tag, tags));
	if (sense.ak || sense.ar) notes.push(`only ${[...(sense.ak ?? []), ...(sense.ar ?? [])].join(", ")}`);
	for (const [lang, word, wasei] of sense.ls ?? []) {
		const language = LANGUAGES[lang] ?? lang;
		notes.push(wasei ? `wasei-eigo${word ? ` from “${word}”` : ""}` : `from ${language}${word ? ` “${word}”` : ""}`);
	}
	notes.push(...(sense.i ?? []));
	return notes;
}

function highlight(sentence: string, form: string): string {
	const html = escape(sentence);
	return form && sentence.includes(form) ? html.replace(escape(form), `<b>${escape(form)}</b>`) : html;
}

function refText(refs: [string, string | null][]): string {
	return refs.map(([written, reading]) => (reading ? `${written}（${reading}）` : written)).join(", ");
}

export function jlptLabel(level: number): string {
	return level ? `N${level}` : "";
}

/** One-line common forms for the detail page: polite, negative, past, te-form. */
function formsLine(rows: ConjugationRow[]): string {
	const pick = (form: string, col: 1 | 2) => rows.find((r) => r[0] === form)?.[col];
	const forms = [pick("Polite", 1), pick("Plain", 2), pick("Past", 1), pick("Te-form", 1)].filter(Boolean) as string[];
	return forms.map(escape).join(" · ");
}

/** The headword and the reading over it, as Jisho shows it. */
function headline(entry: Entry): string {
	const reading = entry.head === entry.reading ? "" : entry.reading;
	return `${reading ? `<span style="font-size:11pt;color:${MUTED}">${escape(reading)}</span><br>` : ""}<span style="font-size:26pt">${escape(entry.head)}</span>`;
}

export function entryMarkdown(hit: Hit, dict: Dictionary): string {
	const { entry } = hit;
	const tags = dict.tags;
	const out: string[] = [headline(entry)];

	const line: string[] = [escape(kanaToRomaji(entry.reading))];
	const accents = entry.pa?.[entry.reading];
	if (accents?.length) {
		line.push(accents.map((a) => `${pitchHtml(entry.reading, a)} <span style="font-size:9pt">${pitchName(entry.reading, a)}</span>`).join(" or "));
	}
	const badges = [
		entry.common ? chip("common", "#7fc8a0") : "",
		entry.jlpt ? chip(`JLPT N${entry.jlpt}`, JLPT_HEX[entry.jlpt]) : "",
		entry.freq ? chip(`#${entry.freq.toLocaleString("en")} by frequency`, MUTED) : "",
	].filter(Boolean);
	out.push(`${muted(line.join(" · "))}${badges.length ? `&nbsp;&nbsp; ${badges.join("&nbsp; ")}` : ""}`);

	if (hit.inflection) {
		out.push(`> [!TIP]\n> <b>${escape(hit.matched)}</b> is the ${escape(hit.inflection)} form of ${escape(entry.head)}`);
	}

	let lastPos = "";
	entry.s.forEach((sense, i) => {
		const pos = posLine(sense.p, tags);
		const block: string[] = [];
		if (pos && pos !== lastPos) block.push(`<span style="color:${ACCENT};font-size:9pt">${escape(pos.toUpperCase())}</span>`);
		lastPos = pos || lastPos;
		const notes = senseNotes(sense, tags);
		block.push(`<b>${i + 1}.</b>&nbsp; ${glossText(sense)}${notes.length ? ` ${muted(`— ${escape(notes.join("; "))}`)}` : ""}`);
		const refs: string[] = [];
		if (sense.rel?.length) refs.push(`see also ${escape(refText(sense.rel))}`);
		if (sense.ant?.length) refs.push(`opposite ${escape(refText(sense.ant))}`);
		if (refs.length) block.push(muted(refs.join(" · ")));
		for (const [ja, en, form] of (sense.x ?? []).slice(0, 2)) {
			block.push(`<span style="color:#c8ccd2">${highlight(ja, form)}</span><br>${muted(escape(en))}`);
		}
		out.push(block.join("<br>"));
	});

	const others = [
		...entry.k.filter((k) => k[0] !== entry.head && !k[2].includes("sK")).map((k) => [k[0], k[2]] as const),
		...entry.r.filter((r) => r[0] !== entry.reading && !r[2].includes("sk")).map((r) => [r[0], r[2]] as const),
	];
	if (others.length) {
		const list = others.map(([text, t]) => `${escape(text)}${t.length ? muted(` (${t.map((x) => tagLabel(x, tags)).join(", ")})`) : ""}`);
		out.push(`<b>Other forms</b>&nbsp; ${list.join("、")}`);
	}

	const table = conjugationTable(entry.head, entry.s[0].p);
	if (table) out.push(`<b>Forms</b>&nbsp; ${formsLine(table)}`);

	const kanji = dict.kanjiIn(entry.head);
	if (kanji.length) {
		out.push(
			kanji
				.map((k) => `<span style="font-size:15pt">${k.c}</span>&nbsp; ${escape(k.m.slice(0, 3).join(", "))} ${muted([k.on.slice(0, 2).join("、"), k.kun.slice(0, 2).join("、")].filter(Boolean).map(escape).join("&nbsp; · &nbsp;"))}`)
				.join("<br>"),
		);
	}
	return out.join("\n\n");
}

/** A verb's or adjective's conjugations as a table. */
export function conjugationMarkdown(entry: Entry, tags: Record<string, string>): string | null {
	const rows = conjugationTable(entry.head, entry.s[0].p);
	if (!rows) return null;
	return [
		headline(entry),
		muted(escape(posLine(entry.s[0].p, tags))),
		"| | Affirmative | Negative |\n|:--|:--|:--|",
	].join("\n\n") + `\n${rows.map(([form, yes, no]) => `| ${form} | ${escape(yes)} | ${escape(no)} |`).join("\n")}`;
}

export function kanjiJlpt(old: number | null): string {
	// The pre-2010 levels: 4 is about N5, 3 N4, 2 spans N3-N2, 1 N1.
	return old ? ({ 4: "N5", 3: "N4", 2: "N3–N2", 1: "N1" } as Record<number, string>)[old] ?? "" : "";
}

/** The kanji's Kangxi radical as a character (⾷ for 184). */
export function radicalChar(n: number | null): string {
	return n ? String.fromCodePoint(0x2f00 + n - 1) : "";
}

export function kanjiMarkdown(k: Kanji, dict: Dictionary): string {
	const out: string[] = [];
	if (k.svg) {
		out.push(`<img src="data:image/svg+xml;base64,${Buffer.from(k.svg).toString("base64")}" width="150" height="150"/>`);
	} else {
		out.push(`<span style="font-size:48pt">${k.c}</span>`);
	}
	out.push(`<span style="font-size:15pt">${escape(k.m.join(", ") || "—")}</span>`);
	const rows: string[] = [];
	if (k.on.length) rows.push(`<b>On</b>&nbsp; ${escape(k.on.join("、"))}`);
	if (k.kun.length) rows.push(`<b>Kun</b>&nbsp; ${escape(k.kun.join("、"))}`);
	if (k.na.length) rows.push(`<b>In names</b>&nbsp; ${escape(k.na.slice(0, 8).join("、"))}`);
	if (rows.length) out.push(rows.join("<br>"));
	const facts = [
		k.sc ? `${k.sc} strokes` : "",
		k.gr ? (k.gr <= 6 ? `grade ${k.gr}` : k.gr === 8 ? "secondary school" : "jinmeiyō") : "",
		kanjiJlpt(k.jl) ? `JLPT ${kanjiJlpt(k.jl)}` : "",
		k.fr ? `#${k.fr} most used` : "",
		k.rad ? `radical ${radicalChar(k.rad)} (${k.rad})` : "",
		k.parts.length ? `parts ${k.parts.join(" ")}` : "",
	].filter(Boolean);
	out.push(muted(escape(facts.join(" · "))));
	const words = dict.wordsWith(k.c, 8);
	if (words.length) {
		out.push(
			`<b>Words</b><br>${words
				.map((w) => `${escape(w.head)} ${muted(escape(w.reading))}&nbsp; ${escape(summary(w))}`)
				.join("<br>")}`,
		);
	}
	return out.join("\n\n");
}

export function translationMarkdown(
	source: string,
	translation: string | null,
	direction: string,
	error?: string,
	words: Segment[] = [],
): string {
	const out = [muted(escape(direction))];
	if (error) out.push(`> [!WARNING]\n> ${escape(error)}`);
	else if (translation === null) out.push(muted("Translating…"));
	else out.push(`<span style="font-size:15pt">${escape(translation).replace(/\n/g, "<br>")}</span>`);
	out.push(`<span style="color:#c8ccd2">${escape(source).replace(/\n/g, "<br>")}</span>`);
	if (words.length) {
		const lines = words.map(({ text, hits: [hit] }) => {
			const e = hit.entry;
			const head = fold(text) === fold(e.head) ? "" : ` → ${e.head}`;
			const reading = e.reading === e.head ? "" : ` ${muted(escape(e.reading))}`;
			const how = hit.inflection ? ` ${muted(`(${escape(hit.inflection)})`)}` : "";
			return `<b>${escape(text + head)}</b>${reading}&nbsp; ${escape(clip(summary(e), 60))}${how}`;
		});
		out.push(`<span style="color:${ACCENT};font-size:9pt">WORD BY WORD</span><br>${lines.join("<br>")}`);
	}
	out.push(muted("Offline machine translation (Firefox Translations models): fine for the gist; check anything important."));
	return out.join("\n\n");
}

/** Whether a character is worth a kanji row of its own. */
export function isListedKanji(c: string): boolean {
	return isKanji(c) && c !== "々";
}
