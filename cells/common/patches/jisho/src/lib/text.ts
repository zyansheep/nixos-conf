// Text helpers: folding queries as build_db.py folds the database, kana and
// romaji conversion, and morae for pitch accents.

/** NFKC, lower case, katakana as hiragana (build_db.py's `fold`). */
export function fold(text: string): string {
	let out = "";
	for (const c of text.normalize("NFKC").toLowerCase()) {
		const code = c.codePointAt(0) as number;
		out +=
			(code >= 0x30a1 && code <= 0x30f6) || code === 0x30fd || code === 0x30fe
				? String.fromCodePoint(code - 0x60)
				: c;
	}
	return out;
}

/** A gloss or query as matched exactly (build_db.py's `english_key`). */
export function englishKey(text: string): string {
	let s = text.toLowerCase();
	// Innermost first: "dog (Canis (lupus) familiaris)".
	for (let before = ""; before !== s; ) {
		before = s;
		s = s.replace(/\([^()]*\)/g, " ");
	}
	s = s.replace(/\s+/g, " ").replace(/^[ .,;:!?"']+|[ .,;:!?"']+$/g, "");
	for (const prefix of ["to ", "a ", "an ", "the "]) {
		if (s.startsWith(prefix)) return s.slice(prefix.length);
	}
	return s;
}

export function isKanji(c: string): boolean {
	const code = c.codePointAt(0) as number;
	return (
		(code >= 0x3400 && code <= 0x9fff) ||
		(code >= 0xf900 && code <= 0xfaff) ||
		(code >= 0x20000 && code <= 0x3134f) ||
		c === "々"
	);
}

export function isKana(c: string): boolean {
	const code = c.codePointAt(0) as number;
	return (code >= 0x3041 && code <= 0x30ff) || (code >= 0xff66 && code <= 0xff9f);
}

/** Whether the text has any kana or kanji. */
export function hasJapanese(text: string): boolean {
	for (const c of text) if (isKana(c) || isKanji(c)) return true;
	return false;
}

export function kanjiOf(text: string): string[] {
	return [...new Set([...text].filter((c) => isKanji(c) && c !== "々"))];
}

export function toKatakana(text: string): string {
	let out = "";
	for (const c of text) {
		const code = c.codePointAt(0) as number;
		out += code >= 0x3041 && code <= 0x3096 ? String.fromCodePoint(code + 0x60) : c;
	}
	return out;
}

// Wāpuro romaji, as typed into an IME: "si" and "shi", "tu" and "tsu", "nn" and
// "n'" for ん, doubled consonants for っ, "-" for ー, "x"/"l" for small kana.
const ROMAJI: Record<string, string> = {};
{
	const rows: Record<string, string> = {
		"": "あいうえお", k: "かきくけこ", g: "がぎぐげご", s: "さしすせそ", z: "ざじずぜぞ",
		t: "たちつてと", d: "だぢづでど", n: "なにぬねの", h: "はひふへほ", b: "ばびぶべぼ",
		p: "ぱぴぷぺぽ", m: "まみむめも", r: "らりるれろ", x: "ぁぃぅぇぉ", l: "ぁぃぅぇぉ",
	};
	for (const [consonant, kana] of Object.entries(rows)) {
		[..."aiueo"].forEach((v, i) => {
			ROMAJI[consonant + v] = kana[i];
		});
	}
	const extra: Record<string, string> = {
		ya: "や", yu: "ゆ", yo: "よ", ye: "いぇ", wa: "わ", wi: "うぃ", we: "うぇ", wo: "を",
		shi: "し", chi: "ち", tsu: "つ", fu: "ふ", ji: "じ", hu: "ふ", si: "し", ti: "ち",
		tu: "つ", zi: "じ", di: "ぢ", du: "づ", va: "ゔぁ", vi: "ゔぃ", vu: "ゔ", ve: "ゔぇ", vo: "ゔぉ",
		fa: "ふぁ", fi: "ふぃ", fe: "ふぇ", fo: "ふぉ", she: "しぇ", che: "ちぇ", je: "じぇ",
		thi: "てぃ", dhi: "でぃ", twu: "とぅ", dwu: "どぅ", tsa: "つぁ", tse: "つぇ", tso: "つぉ",
		xya: "ゃ", xyu: "ゅ", xyo: "ょ", lya: "ゃ", lyu: "ゅ", lyo: "ょ", xtu: "っ", ltu: "っ",
		xtsu: "っ", ltsu: "っ", xwa: "ゎ", lwa: "ゎ", xka: "ゕ", xke: "ゖ", "n'": "ん", nn: "ん",
		"-": "ー", ",": "、", ".": "。",
	};
	Object.assign(ROMAJI, extra);
	const yoon: Record<string, string> = {
		k: "き", g: "ぎ", s: "し", z: "じ", t: "ち", d: "ぢ", n: "に", h: "ひ", b: "び",
		p: "ぴ", m: "み", r: "り", c: "ち", j: "じ",
	};
	for (const [consonant, kana] of Object.entries(yoon)) {
		const small = { a: "ゃ", u: "ゅ", o: "ょ", e: "ぇ" };
		for (const [v, s] of Object.entries(small)) {
			if (consonant !== "c" && consonant !== "j") ROMAJI[`${consonant}y${v}`] = kana + s;
		}
	}
	for (const [v, s] of Object.entries({ a: "ゃ", u: "ゅ", o: "ょ" })) {
		ROMAJI[`sh${v}`] = `し${s}`;
		ROMAJI[`ch${v}`] = `ち${s}`;
		ROMAJI[`cy${v}`] = `ち${s}`;
		ROMAJI[`j${v}`] = `じ${s}`;
		ROMAJI[`jy${v}`] = `じ${s}`;
	}
}

/** Romaji as hiragana, or null when some of it isn't romaji ("taberu" -> "たべる"). */
export function romajiToKana(text: string): string | null {
	const s = text.toLowerCase().trim();
	if (!/^[a-z' .,-]+$/.test(s) || !/[aiueo]/.test(s)) return null;
	let out = "";
	let i = 0;
	while (i < s.length) {
		const c = s[i];
		if (c === " ") {
			i++;
			continue;
		}
		// A doubled consonant: っ (but "nn" is ん).
		if (c === s[i + 1] && /[bcdfghjkmpqrstvwxyz]/.test(c)) {
			out += "っ";
			i++;
			continue;
		}
		if (c === "t" && s.startsWith("ch", i + 1)) {
			out += "っ";
			i++;
			continue;
		}
		// ん: "n" before a consonant or at the end, "nn" unless a vowel follows
		// (then the second "n" starts the next syllable: "konnichiwa"), "n'".
		if (c === "n" && !/[aiueoy]/.test(s[i + 1] ?? "")) {
			const after = s[i + 2] ?? "";
			out += "ん";
			i += s[i + 1] === "'" || (s[i + 1] === "n" && !/[aiueoy]/.test(after)) ? 2 : 1;
			continue;
		}
		let matched = false;
		for (const len of [4, 3, 2, 1]) {
			const kana = ROMAJI[s.slice(i, i + len)];
			if (kana) {
				out += kana;
				i += len;
				matched = true;
				break;
			}
		}
		if (matched) continue;
		return null;
	}
	return out;
}

const KANA_ROMAJI: Record<string, string> = {};
for (const [romaji, kana] of Object.entries(ROMAJI)) {
	// The first spelling listed wins: "shi", not "si"; "tsu", not "tu".
	if (/^[a-z]+$/.test(romaji) && !KANA_ROMAJI[kana] && !/^[xl]/.test(romaji)) {
		KANA_ROMAJI[kana] = romaji;
	}
}
Object.assign(KANA_ROMAJI, {
	し: "shi", ち: "chi", つ: "tsu", ふ: "fu", じ: "ji", ぢ: "ji", づ: "zu", を: "wo",
	しゃ: "sha", しゅ: "shu", しょ: "sho", ちゃ: "cha", ちゅ: "chu", ちょ: "cho",
	じゃ: "ja", じゅ: "ju", じょ: "jo", ん: "n", ぁ: "a", ぃ: "i", ぅ: "u", ぇ: "e", ぉ: "o",
	ゃ: "ya", ゅ: "yu", ょ: "yo", ゎ: "wa", ゔ: "vu",
});

/** Kana as wāpuro romaji, as you'd type it ("とうきょう" -> "toukyou"). */
export function kanaToRomaji(text: string): string {
	const s = fold(text);
	let out = "";
	for (let i = 0; i < s.length; i++) {
		const two = KANA_ROMAJI[s.slice(i, i + 2)];
		if (two && s.length > i + 1 && "ゃゅょぁぃぅぇぉ".includes(s[i + 1])) {
			out += two;
			i++;
			continue;
		}
		const c = s[i];
		if (c === "っ") {
			const next = KANA_ROMAJI[s.slice(i + 1, i + 3)] ?? KANA_ROMAJI[s[i + 1]] ?? "";
			out += next.startsWith("ch") ? "t" : next[0] && /[^aiueon]/.test(next[0]) ? next[0] : "";
			continue;
		}
		if (c === "ー") {
			out += out.at(-1) && /[aiueo]/.test(out.at(-1) as string) ? (out.at(-1) as string) : "-";
			continue;
		}
		if (c === "ん" && /^[aiueoy]/.test(KANA_ROMAJI[s[i + 1]] ?? "")) {
			out += "n'";
			continue;
		}
		out += KANA_ROMAJI[c] ?? c;
	}
	return out;
}

/** The reading split into morae: small kana join the one before; っ, ー and ん count. */
export function morae(reading: string): string[] {
	const out: string[] = [];
	for (const c of reading) {
		if ("ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ".includes(c) && out.length) out[out.length - 1] += c;
		else out.push(c);
	}
	return out;
}
