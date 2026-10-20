// Conjugation both ways, from one table: `deinflect` takes 食べさせられなかった
// back to 食べる (as causative · passive · negative · past) so lookups find
// inflected words, and `conjugationTable` lists a verb's or adjective's forms.
//
// A rule turns an inflected ending back into its base: `from` -> `to`. `in`
// is what the inflected word is itself (its own class, when it conjugates
// further: 食べない is an i-adjective; none when it doesn't: 食べた), and `out`
// the class of the base. Deinflecting applies rules while each one's `in`
// meets the class the previous step needs, so a chain only ever runs through
// real forms; a result counts when the dictionary word has that class.

export type Rule = { from: string; to: string; in: string[]; out: string[]; reason: string };

type Stems = {
	dict: string; // The dictionary-form ending the others replace.
	a: string; // Before ない.
	az: string; // Before ず, ぬ.
	i: string; // Before ます (the masu stem).
	te: string;
	ta: string;
	pot: string[];
	pass: string[];
	caus: string[];
	cpass: string[];
	vol: string;
	imp: string[];
	cond: string;
};

function godan(dict: string, a: string, i: string, e: string, o: string, te: string, ta: string): Stems {
	return {
		dict, a, az: a, i, te, ta,
		pot: [`${e}る`], pass: [`${a}れる`], caus: [`${a}せる`],
		cpass: dict === "す" ? [`${a}せられる`] : [`${a}せられる`, `${a}される`],
		vol: `${o}う`, imp: [e], cond: `${e}ば`,
	};
}

const ICHIDAN: Stems = {
	dict: "る", a: "", az: "", i: "", te: "て", ta: "た", pot: ["られる", "れる"], pass: ["られる"],
	caus: ["させる"], cpass: ["させられる"], vol: "よう", imp: ["ろ", "よ"], cond: "れば",
};

function kuru(k: string, ki: string, ko: string): Stems {
	return {
		dict: `${k}る`, a: ko, az: ko, i: ki, te: `${ki}て`, ta: `${ki}た`,
		pot: [`${ko}られる`, `${ko}れる`], pass: [`${ko}られる`], caus: [`${ko}させる`],
		cpass: [`${ko}させられる`], vol: `${ko}よう`, imp: [`${ko}い`], cond: `${k}れば`,
	};
}

/** Stem tables by JMdict class; a class can have more than one (来る and くる). */
export const STEMS: Record<string, Stems[]> = {
	// Irregular verbs first: where two analyses meet, the first found is kept, and
	// 行ってきます is 行ってくる's polite form before it's 行ってく's.
	vk: [kuru("く", "き", "こ"), kuru("来", "来", "来")],
	"vs-i": [
		{
			dict: "する", a: "し", az: "せ", i: "し", te: "して", ta: "した", pot: [], pass: ["される"],
			caus: ["させる"], cpass: ["させられる"], vol: "しよう", imp: ["しろ", "せよ"], cond: "すれば",
		},
	],
	"vs-s": [
		{
			dict: "する", a: "さ", az: "せ", i: "し", te: "して", ta: "した", pot: [], pass: ["される"],
			caus: ["させる"], cpass: ["させられる"], vol: "しよう", imp: ["しろ", "せよ"], cond: "すれば",
		},
	],
	vz: [
		{
			dict: "ずる", a: "じ", az: "ぜ", i: "じ", te: "じて", ta: "じた", pot: [], pass: ["じられる", "ぜられる"],
			caus: ["じさせる"], cpass: ["じさせられる"], vol: "じよう", imp: ["じろ", "ぜよ"], cond: "ずれば",
		},
	],
	v1: [ICHIDAN],
	"v1-s": [{ ...ICHIDAN, imp: [""] }],
	v5u: [godan("う", "わ", "い", "え", "お", "って", "った")],
	"v5u-s": [godan("う", "わ", "い", "え", "お", "うて", "うた")],
	v5k: [godan("く", "か", "き", "け", "こ", "いて", "いた")],
	"v5k-s": [godan("く", "か", "き", "け", "こ", "って", "った")],
	v5g: [godan("ぐ", "が", "ぎ", "げ", "ご", "いで", "いだ")],
	v5s: [godan("す", "さ", "し", "せ", "そ", "して", "した")],
	v5t: [godan("つ", "た", "ち", "て", "と", "って", "った")],
	v5n: [godan("ぬ", "な", "に", "ね", "の", "んで", "んだ")],
	v5b: [godan("ぶ", "ば", "び", "べ", "ぼ", "んで", "んだ")],
	v5m: [godan("む", "ま", "み", "め", "も", "んで", "んだ")],
	v5r: [godan("る", "ら", "り", "れ", "ろ", "って", "った")],
	"v5r-i": [godan("る", "ら", "り", "れ", "ろ", "って", "った")],
	v5aru: [{ ...godan("る", "ら", "い", "れ", "ろ", "って", "った"), imp: ["い"] }],
};

// [reason, endings from the stems, the class of the inflected word or null].
const VERB_FORMS: [string, (s: Stems) => string[], string | null][] = [
	["negative", (s) => [`${s.a}ない`], "adj-i"],
	["polite", (s) => [`${s.i}ます`], "masu"],
	["past", (s) => [s.ta], null],
	["te-form", (s) => [s.te], "te"],
	["potential", (s) => s.pot, "v1"],
	["passive", (s) => s.pass, "v1"],
	["causative", (s) => s.caus, "v1"],
	["causative passive", (s) => s.cpass.slice(1), "v1"], // The long form chains: causative · passive.
	["volitional", (s) => [s.vol], null],
	["imperative", (s) => s.imp, null],
	["conditional", (s) => [s.cond], null],
	["conditional (~たら)", (s) => [`${s.ta}ら`], null],
	["~たり", (s) => [`${s.ta}り`], null],
	["want to (~たい)", (s) => [`${s.i}たい`], "adj-i"],
	["negative (~ず)", (s) => [`${s.az}ず`, `${s.az}ずに`], null],
	["negative (~ぬ)", (s) => [`${s.az}ぬ`], null],
	["negative (~ん)", (s) => [`${s.a}ん`], null],
	["without doing (~ないで)", (s) => [`${s.a}ないで`], null],
	["command (~なさい)", (s) => [`${s.i}なさい`], null],
	["looks like (~そう)", (s) => [`${s.i}そう`], null],
	["while (~ながら)", (s) => [`${s.i}ながら`], null],
	["too much (~すぎる)", (s) => [`${s.i}すぎる`, `${s.i}過ぎる`], "v1"],
	["easy to (~やすい)", (s) => [`${s.i}やすい`], "adj-i"],
	["hard to (~にくい)", (s) => [`${s.i}にくい`], "adj-i"],
	["way of (~方)", (s) => [`${s.i}方`, `${s.i}かた`], null],
	["masu stem", (s) => [s.i], null],
];

const MASU_FORMS: [string, string][] = [
	["ません", "polite negative"],
	["ました", "polite past"],
	["ませんでした", "polite negative past"],
	["ましょう", "polite volitional"],
	["まして", "polite te-form"],
	["ませ", "polite imperative"],
	["ましたら", "polite conditional"],
];

// After a te-form: [ending, reason, the class of the whole].
const TE_FORMS: [string, string, string | null][] = [
	["いる", "progressive (~ている)", "v1"],
	["る", "progressive (~てる)", "v1"],
	["おる", "progressive (~ておる)", "v5r"],
	["ある", "resultative (~てある)", "v5r-i"],
	["おく", "in advance (~ておく)", "v5k"],
	["しまう", "completed (~てしまう)", "v5u"],
	["いく", "~ていく", "v5k-s"],
	["く", "~てく", "v5k-s"],
	["くる", "~てくる", "vk"],
	["みる", "try (~てみる)", "v1"],
	["あげる", "for someone (~てあげる)", "v1"],
	["くれる", "for me (~てくれる)", "v1-s"],
	["もらう", "have done (~てもらう)", "v5u"],
	["くださる", "please (~てくださる)", "v5aru"],
	["ください", "please (~てください)", null],
	["ほしい", "want done (~てほしい)", "adj-i"],
	["も", "even if (~ても)", null],
	["は", "~ては", null],
];
// Contractions of a te-form and what follows: [て version, で version, reason, class].
const TE_CONTRACTIONS: [string, string, string, string | null][] = [
	["ちゃう", "じゃう", "completed (~ちゃう)", "v5u"],
	["ちまう", "じまう", "completed (~ちまう)", "v5u"],
	["とく", "どく", "in advance (~とく)", "v5k"],
	["ちゃ", "じゃ", "~ちゃ (~ては)", null],
];

// i-adjectives: [ending replacing い, reason, class of the inflected word].
const ADJECTIVE_FORMS: [string, string, string | null][] = [
	["くない", "negative", "adj-i"],
	["かった", "past", null],
	["くて", "te-form", null],
	["くても", "even if (~ても)", null],
	["く", "adverb", null],
	["ければ", "conditional", null],
	["けりゃ", "conditional (~けりゃ)", null],
	["きゃ", "conditional (~きゃ)", null],
	["くちゃ", "~くちゃ (~くては)", null],
	["かったら", "conditional (~たら)", null],
	["かったり", "~たり", null],
	["さ", "noun (~さ)", null],
	["そう", "looks like (~そう)", null],
	["すぎる", "too (~すぎる)", "v1"],
	["過ぎる", "too (~すぎる)", "v1"],
	["くありません", "polite negative", null],
	["くありませんでした", "polite negative past", null],
	["かろう", "presumptive (~かろう)", null],
];

function buildRules(): Rule[] {
	const rules: Rule[] = [];
	const add = (from: string, to: string, inClass: string | null, out: string, reason: string) =>
		rules.push({ from, to, in: inClass ? [inClass] : [], out: [out], reason });
	for (const [cls, tables] of Object.entries(STEMS)) {
		for (const s of tables) {
			// One rule per ending: ichidan られる is "potential or passive".
			const endings = new Map<string, { reasons: string[]; inClass: string | null }>();
			for (const [reason, make, inClass] of VERB_FORMS) {
				for (const ending of make(s)) {
					const known = endings.get(ending);
					if (!known) endings.set(ending, { reasons: [reason], inClass });
					else if (!known.reasons.includes(reason)) known.reasons.push(reason);
				}
			}
			// (The ichidan masu stem is the bare stem: its rule, from "", fits every word.)
			for (const [ending, { reasons, inClass }] of endings) add(ending, s.dict, inClass, cls, reasons.join(" or "));
		}
	}
	for (const [ending, reason] of MASU_FORMS) add(ending, "ます", null, "masu", reason);
	for (const [after, reason, cls] of TE_FORMS) {
		add(`て${after}`, "て", cls, "te", reason);
		add(`で${after}`, "で", cls, "te", reason);
	}
	for (const [te, de, reason, cls] of TE_CONTRACTIONS) {
		add(te, "て", cls, "te", reason);
		add(de, "で", cls, "te", reason);
	}
	for (const [ending, reason, cls] of ADJECTIVE_FORMS) add(ending, "い", cls, "adj-i", reason);
	// ない and よい look-like forms keep an さ: なさそう, よさそう.
	add("なさそう", "ない", null, "adj-i", "looks like (~そう)");
	add("よさそう", "よい", null, "adj-i", "looks like (~そう)");
	return rules;
}

export const RULES = buildRules();
const BY_LAST = new Map<string, Rule[]>();
const FROM_EMPTY: Rule[] = [];
for (const rule of RULES) {
	if (!rule.from) {
		FROM_EMPTY.push(rule);
		continue;
	}
	const last = rule.from.at(-1) as string;
	BY_LAST.set(last, [...(BY_LAST.get(last) ?? []), rule]);
}

/** The classes rules know a JMdict part of speech by. */
export function classesOf(pos: string[]): string[] {
	const out = new Set<string>();
	for (const p of pos) {
		if (p === "adj-ix") out.add("adj-i");
		if (p in STEMS || p === "adj-i") out.add(p);
	}
	return [...out];
}

export type Deinflection = {
	text: string;
	/** The classes the dictionary word must have; null for the text as typed. */
	types: string[] | null;
	/** Reasons, from the dictionary form outward. */
	reasons: string[];
};

/** Every base the text could be an inflection of, the text itself first. */
export function deinflect(text: string): Deinflection[] {
	const results: Deinflection[] = [{ text, types: null, reasons: [] }];
	const seen = new Set([`${text}|*`]);
	for (let i = 0; i < results.length && results.length < 400; i++) {
		const { text: current, types, reasons } = results[i];
		if (reasons.length >= 7) continue;
		const candidates = [...(BY_LAST.get(current.at(-1) ?? "") ?? []), ...FROM_EMPTY];
		for (const rule of candidates) {
			if (!current.endsWith(rule.from)) continue;
			if (types && !rule.in.some((t) => types.includes(t))) continue;
			const base = current.slice(0, current.length - rule.from.length) + rule.to;
			if (!base || base === rule.to && rule.to.length < 2 && rule.from === "") continue;
			const key = `${base}|${rule.out.join(",")}`;
			if (seen.has(key)) continue;
			seen.add(key);
			results.push({ text: base, types: rule.out, reasons: [rule.reason, ...reasons] });
		}
	}
	return results;
}

/** The reasons as read: "polite" folds into "polite past"; a te-form into ~ている. */
export function describe(reasons: string[]): string {
	const out: string[] = [];
	reasons.forEach((reason, i) => {
		const next = reasons[i + 1];
		if (reason === "polite" && next?.startsWith("polite ")) return;
		if (reason === "te-form" && next && /~て|~で|~ちゃ|~ちま|~とく|~じゃ|~じま/.test(next)) return;
		out.push(reason);
	});
	return out.join(" · ");
}

export type ConjugationRow = [form: string, affirmative: string, negative: string];

/** The forms of a verb or i-adjective (`written` in its dictionary form), or null. */
export function conjugationTable(written: string, pos: string[]): ConjugationRow[] | null {
	const cls = pos.find((p) => p in STEMS || p === "adj-i" || p === "adj-ix" || p === "adj-na" || p === "vs");
	if (!cls) return null;
	if (cls === "adj-i" || cls === "adj-ix") {
		const stem = cls === "adj-ix" ? `${written.slice(0, -2)}${written.endsWith("いい") ? "よ" : written.at(-2)}` : written.slice(0, -1);
		if (!written.endsWith("い")) return null;
		return [
			["Plain", written, `${stem}くない`],
			["Polite", `${written}です`, `${stem}くないです`],
			["Past", `${stem}かった`, `${stem}くなかった`],
			["Polite past", `${stem}かったです`, `${stem}くなかったです`],
			["Te-form", `${stem}くて`, `${stem}くなくて`],
			["Adverb", `${stem}く`, "—"],
			["Conditional", `${stem}ければ`, `${stem}くなければ`],
			["Conditional (~たら)", `${stem}かったら`, `${stem}くなかったら`],
			["Looks like", `${stem}そう`, `${stem}くなさそう`],
			["Noun", `${stem}さ`, "—"],
		];
	}
	if (cls === "adj-na") {
		return [
			["Plain", `${written}だ`, `${written}じゃない`],
			["Polite", `${written}です`, `${written}じゃありません`],
			["Past", `${written}だった`, `${written}じゃなかった`],
			["Polite past", `${written}でした`, `${written}じゃありませんでした`],
			["Te-form", `${written}で`, `${written}じゃなくて`],
			["Before a noun", `${written}な`, "—"],
			["Adverb", `${written}に`, "—"],
			["Conditional", `${written}なら`, `${written}じゃなければ`],
		];
	}
	const verb = cls === "vs" ? `${written}する` : written;
	const tables = STEMS[cls === "vs" ? "vs-i" : cls];
	const s = tables.find((t) => verb.endsWith(t.dict));
	if (!s) return null;
	const root = verb.slice(0, verb.length - s.dict.length);
	const w = (ending: string) => root + ending;
	// The plain negative and its forms; ある's is ない.
	const nai = cls === "v5r-i" ? "な" : w(`${s.a}な`);
	const v1 = (form: string): [string, string] => [form, `${form.slice(0, -1)}ない`];
	const rows: ConjugationRow[] = [
		["Plain", verb, `${nai}い`],
		["Polite", w(`${s.i}ます`), w(`${s.i}ません`)],
		["Past", w(s.ta), `${nai}かった`],
		["Polite past", w(`${s.i}ました`), w(`${s.i}ませんでした`)],
		["Te-form", w(s.te), `${nai}くて`],
	];
	if (s.pot.length) rows.push(["Potential", ...v1(w(s.pot[0]))]);
	else if (cls === "vs" || cls === "vs-i") rows.push(["Potential", ...v1(cls === "vs" ? `${written}できる` : "できる")]);
	rows.push(
		["Passive", ...v1(w(s.pass[0]))],
		["Causative", ...v1(w(s.caus[0]))],
		["Causative passive", ...v1(w(s.cpass[0]))],
		["Volitional", w(s.vol), `${verb}まい`],
		["Imperative", w(s.imp[0]), `${verb}な`],
		["Conditional", w(s.cond), `${nai}ければ`],
		["Conditional (~たら)", w(`${s.ta}ら`), `${nai}かったら`],
		["Want to", w(`${s.i}たい`), w(`${s.i}たくない`)],
	);
	return rows;
}
