// The text styles, after lingojam.com/CoolTextFonts: its character maps,
// frames and emoji are in lingojam.json (see ../README.md), the code that
// applies them is rewritten here to work on code points and to take its
// randomness from a seed, so a random style holds still while you type.
import data from "./lingojam.json" with { type: "json" };

type Rand = () => number;
type CharMap = Record<string, string>;

export interface Style {
	id: string;
	name: string;
	/** Picks something at random: re-rolled with Ctrl+R. */
	random?: boolean;
	render(text: string, rand: Rand): string;
}

const chars = (s: string) => Array.from(s);
const pick = <T>(rand: Rand, a: ArrayLike<T>): T => a[Math.floor(rand() * a.length)];
const reverse = (s: string) => chars(s).reverse().join("");

// lingojam's applyCharMap: a character, else its lowercase, else itself.
export function mapChars(map: CharMap, text: string): string {
	return chars(text)
		.map((c) => map[c] ?? map[c.toLowerCase()] ?? c)
		.join("");
}

const maps = data.charMaps as Record<string, CharMap>;
const lunicode = data.lunicode as { flip: CharMap; mirror: CharMap; tiny: CharMap; bubbles: CharMap; creepify: Record<"top" | "middle" | "bottom", string[]> };

// ---- Lunicode (lunicode.com, as lingojam uses it) ---------------------------

const tiny = (t: string) => chars(t.toUpperCase()).map((c) => lunicode.tiny[c] ?? c).join("");
const flip = (t: string) => reverse(chars(t).map((c) => lunicode.flip[c] ?? c).join(""));
const mirror = (t: string) =>
	t.split("\n").map((line) => reverse(chars(line).map((c) => lunicode.mirror[c] ?? c).join(""))).join("\n");

// Enclose each character in a combining mark; ones after the first on a line
// get a little space before them, or the marks overlap.
function enclose(t: string, mark: string, gap: string, map: CharMap = {}): string {
	let out = "";
	let lineStart = true;
	for (const c of chars(t)) {
		let s = map[c];
		if (s === undefined) {
			if (c.codePointAt(0)! >= 33) s = (lineStart ? "" : gap) + c + mark;
			else s = c;
		}
		out += s;
		lineStart = s === "\n";
	}
	return out;
}

function creepify(t: string, rand: Rand, maxHeight = 10): string {
	const { top, middle, bottom } = lunicode.creepify;
	const height = () => maxHeight - rand() * maxHeight;
	return chars(t)
		.map((c) => {
			let s = c + pick(rand, middle);
			for (let n = 0, h = height(); n < h; n++) s += pick(rand, top);
			for (let n = 0, h = height(); n < h; n++) s += pick(rand, bottom);
			return s;
		})
		.join("");
}

// ---- lingojam's own ---------------------------------------------------------

const join = (t: string, sep: string) => chars(t).join(sep);
const markEach = (t: string, mark: string) => chars(t).join(mark) + mark;
const frameEach = (t: string, open: string, close: string) => chars(t).map((c) => (/\s/.test(c) ? c : open + c + close)).join("");
const frame = (t: string, f: string) => f.replace("[[text]]", t);

const scriptify = (t: string) => mapChars(data.script, t);

// Each character's choices: a list, or a string of single characters.
const crazy: Record<string, string[]> = Object.fromEntries(
	Object.entries(data.crazy as Record<string, string | string[]>).map(([c, v]) => [c, typeof v === "string" ? chars(v) : v]),
);
const crazify = (t: string, rand: Rand) => chars(t.toLowerCase()).map((c) => (crazy[c] ? pick(rand, crazy[c]) : c)).join("");
const symbols = (rand: Rand, n: number) => Array.from({ length: n }, () => pick(rand, data.symbols)).join("");
const withSymbols = (t: string, rand: Rand) => `${symbols(rand, 2)}  ${t}  ${symbols(rand, 2)}`;
const withFlourish = (t: string, rand: Rand) => frame(t, pick(rand, data.flourishFrames));

// "Cute": round letters become flowers and hearts, :) becomes a kaomoji,
// script letters, all between two bits of food, twinkles, animals or flourish.
function cute(t: string, rand: Rand): string {
	const n = Math.floor(rand() * 2) + 1;
	const strings = [
		() => Array.from({ length: n }, () => pick(rand, data.food)).join(" ⋆ "),
		() => Array.from({ length: n }, () => pick(rand, data.twinkles)).join(""),
		() => Array.from({ length: n }, () => pick(rand, data.animals)).join(" ⋆ "),
		() => pick(rand, data.flourishBounds),
	];
	const bounds = pick(rand, strings)();
	let quote = 0;
	const emoji = data.emoji as Record<string, string[]>;
	const word = (w: string) => {
		if (w === "" || /^[\s,!?.]+$/.test(w)) return w;
		w = chars(w).map((c) => (c.toLowerCase() === "o" || c === "0" ? pick(rand, data.round) : c)).join("");
		w = chars(w)
			.map((c) => {
				if (c === "!") return pick(rand, ["❣", "❢"]);
				if (c === "?") return pick(rand, ["¿", "?"]);
				if (c === '"') return ["❝", "❞"][quote++ % 2];
				return c;
			})
			.join("");
		for (const [from, key] of [[":)", ":)"], ["(:", ":)"], ["^^", "^^"], [":P", ":p"], [":p", ":p"], [":D", ":D"], ["<3", "<3"], ["(y)", "(y)"], ["(Y)", "(y)"]] as const)
			w = w.replace(from, () => pick(rand, emoji[key]));
		return scriptify(w);
	};
	const body = t.split(/([!?.]+)/).map((s) => s.split(/([\s,]+)/).map(word).join("")).join("");
	return `${bounds}  🎀  ${body}  🎀  ${reverse(bounds)}`;
}

function vaporKana(rand: Rand, words: number): string {
	const kana = chars(data.vaporwaveKana);
	let s = Array.from({ length: Math.max(3, words) }, () => pick(rand, kana)).join("");
	if (words > 6) s = chars(s).map((c) => c + (rand() < 0.3 ? "　" : "")).join("");
	return s;
}
const vaporwide = (t: string) => mapChars(maps.vaporwave, t);
const words = (t: string) => t.split(" ").length;

// ---- The list ---------------------------------------------------------------

const charMap = (id: string, name: string, map = id): Style => ({ id, name, render: (t) => mapChars(maps[map], t) });
const plain = (id: string, name: string, render: (t: string) => string): Style => ({ id, name, render });
const random = (id: string, name: string, render: (t: string, rand: Rand) => string): Style => ({ id, name, random: true, render });
// A few of lingojam's frames by name; "Crazy framed" picks from all of them.
const framed = (id: string, name: string, start: string): Style => {
	const f = data.flourishFrames.find((f) => f.startsWith(start));
	if (!f) throw new Error(`no lingojam frame starts with ${start}`);
	return plain(id, name, (t) => frame(t, f));
};

// In lingojam's order, then its random repeats as numbered variants.
export const STYLES: Style[] = [
	charMap("oldEnglish", "Old English"),
	charMap("medieval", "Medieval"),
	random("crazy", "Crazy", (t, r) => withSymbols(crazify(t, r), r)),
	charMap("cursive", "Cursive"),
	plain("script", "Script", scriptify),
	charMap("doubleStruck", "Double-struck"),
	charMap("wideText", "Wide"),
	charMap("taiViet", "Tai Viet"),
	charMap("cuteDotsAndScribbles", "Dots & scribbles"),
	charMap("symbolQ", "Little symbols"),
	charMap("yiNuosu", "Yi"),
	random("cute", "Cute", cute),
	plain("tiny", "Tiny", tiny),
	plain("flip", "Upside down", flip),
	plain("roundSquares", "Keycaps", (t) => enclose(t, "⃣", "   ")),
	charMap("blueBold", "Flag letters"),
	plain("squaresOutline", "Squares", (t) => enclose(t, "⃞", "    ")),
	charMap("squares", "Boxed"),
	plain("mirror", "Mirror", mirror),
	random("creepy", "Creepy", (t, r) => creepify(t, r)),
	charMap("invertedSquares", "Inverted boxes"),
	charMap("subscript", "Subscript"),
	charMap("superscript", "Superscript"),
	plain("bubbles", "Bubbles", (t) => enclose(t, "⃝", "    ", lunicode.bubbles)),
	charMap("squiggle", "Squiggle"),
	charMap("squiggle2", "Squiggle 2"),
	charMap("squiggle3", "Squiggle 3"),
	charMap("squiggle4", "Squiggle 4"),
	charMap("squiggle5", "Squiggle 5"),
	charMap("squiggle6", "Squiggle 6"),
	charMap("bold", "Bold"),
	charMap("boldSans", "Bold sans"),
	charMap("italic", "Italic"),
	charMap("boldItalic", "Bold italic"),
	charMap("monospace", "Monospace"),
	charMap("upperAngles", "Upper angles"),
	charMap("greek", "Greek"),
	charMap("symbols", "Symbols"),
	charMap("currency", "Currency"),
	charMap("asianStyle", "Asian style"),
	charMap("asianStyle2", "Asian style 2"),
	plain("thickBlock", "Thick block framed", (t) => frameEach(t, "【", "】")),
	plain("diametric", "Diametric angle framed", (t) => frameEach(t, "『", "』")),
	plain("wavy", "Wavy joiner", (t) => `≋${join(t, "≋")}≋`),
	plain("dotty", "Dotty joiner", (t) => `░${join(t, "░")}░`),
	plain("kirby", "Kirby hug", (t) => `(っ◔◡◔)っ ♥ ${t} ♥`),
	random("vaporwave", "Vaporwave", (t, r) => `${vaporwide(t)}　${vaporKana(r, words(t))}`),
	random("vaporwave2", "Vaporwave 2", (t, r) =>
		`${vaporwide(t).replace(/　/g, "░").replace(/ａｅ/, "æ").replace(/Ａ/g, "Λ").replace(/Ｅ/g, () => (r() > 0.5 ? "Ξ" : "Σ")).replace(/Ｏ/g, "♢")}　（${vaporKana(r, words(t))}）`),
	plain("vaporwave3", "Vaporwave 3", (t) => `【${vaporwide(t)}】`),
	plain("sparkles", "Little sparkles", (t) => `˜”*°•.˜”*°• ${t} •°*”˜.•°*”˜`),
	plain("weirdBox", "Weird box", (t) => t.replace(/(\S)/gu, "[̲̅$1]")),
	plain("firework", "Firework", (t) => markEach(t, "҉")),
	charMap("bentText", "Bent"),
	charMap("neon", "Neon"),
	charMap("futureAlien", "Future alien"),
	plain("strike", "Strikethrough", (t) => markEach(t, "̶")),
	plain("tildeStrike", "Tilde strikethrough", (t) => markEach(t, "̴")),
	plain("slash", "Slashed", (t) => markEach(t, "̷")),
	plain("underline", "Underline", (t) => markEach(t, "̲")),
	plain("doubleUnderline", "Double underline", (t) => markEach(t, "̳")),
	plain("stinky", "Stinky", (t) => markEach(t, "̾")),
	plain("hearts", "Hearts between", (t) => join(t, "♥")),
	plain("arrowBelow", "Arrow below", (t) => markEach(t, "͎")),
	plain("cross", "Cross above & below", (t) => markEach(t, "͓̽")),
	charMap("wingdings", "Wingdings"),
	framed("starWave", "★彡 Star wave", "★彡"),
	framed("gradient", "░▒▓█ Gradient", "░▒▓█ "),
	framed("bars", "ıllıllı Bars", "ıllıllı"),
	framed("rising", "▁▂▄▅▆▇█ Rising", "▁ ▂"),
	framed("music", "♫ Music", "·.¸¸.·♩"),
	framed("flowers", "✿ Flowers", "✿.｡"),
	framed("hug", "ღ(¯`◕‿◕´¯) Happy", "ღ"),
	framed("ribbon", "•´¯`•. Ribbon", "•´¯`•. "),
	framed("arrows", "➶ Arrows", "➶"),
	framed("castle", "ஜ۩۞۩ஜ Castle", "ஜ"),
	framed("wings", "╚»★«╝ Wings", "╚»"),
	...[2, 3].map((n) => random(`cute${n}`, `Cute ${n}`, cute)),
	...[1, 2, 3, 4, 5, 6].map((n) =>
		random(`crazyFramed${n}`, `Crazy framed ${n}`, (t, r) => (r() < 0.7 ? withSymbols(crazify(t, r), r) : withFlourish(crazify(t, r), r))),
	),
];

// ---- Seeded randomness ------------------------------------------------------

// mulberry32, seeded per style so one style's picks don't shift another's.
export function rng(seed: number, id: string): Rand {
	let a = seed >>> 0;
	for (const c of id) a = Math.imul(a ^ c.codePointAt(0)!, 0x9e3779b1);
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export const render = (style: Style, text: string, seed: number) => style.render(text, rng(seed, style.id));
