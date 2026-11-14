// Your own styles, from $XDG_CONFIG_HOME/fancy-text/styles.json: a list of
//
//   { "name": "Sparkly bold", "base": "Bold", "frame": "✧･ﾟ {text} ﾟ･✧" }
//
// Each step is optional and they run in this order:
//   base   another style (by name or id) applied first
//   from + to   a character alphabet: the nth character of `to` replaces the
//          nth of `from` ("abc" → "ᗩᗷᑕ"); or `map`, an object of strings
//   mark   a combining mark put after every character ("̅", overline)
//   join   put between the characters ("·")
//   frame  around the text, at {text} ("★彡 {text} 彡★")
// Read when the command opens; a broken file shows as one row saying why.
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { mapChars, STYLES, type Style } from "./styles.ts";

export interface StyleSpec {
	name: string;
	base?: string;
	from?: string;
	to?: string;
	map?: Record<string, string>;
	mark?: string;
	join?: string;
	frame?: string;
}

export const configPath = () => join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "fancy-text", "styles.json");

export function customStyle(spec: StyleSpec, styles: Style[] = STYLES): Style {
	if (typeof spec?.name !== "string" || !spec.name) throw new Error("every style needs a \"name\"");
	const base = spec.base === undefined ? undefined : styles.find((s) => s.id === spec.base || s.name.toLowerCase() === spec.base!.toLowerCase());
	if (spec.base !== undefined && !base) throw new Error(`${spec.name}: no style called "${spec.base}"`);
	let map = spec.map;
	if (spec.from !== undefined || spec.to !== undefined) {
		const from = Array.from(spec.from ?? "");
		const to = Array.from(spec.to ?? "");
		if (from.length !== to.length) throw new Error(`${spec.name}: "from" has ${from.length} characters but "to" has ${to.length}`);
		map = { ...map, ...Object.fromEntries(from.map((c, i) => [c, to[i]])) };
	}
	if (spec.frame !== undefined && !spec.frame.includes("{text}")) throw new Error(`${spec.name}: "frame" needs {text} in it`);
	return {
		id: `custom:${spec.name}`,
		name: spec.name,
		random: base?.random,
		render(text, rand) {
			let t = base ? base.render(text, rand) : text;
			if (map) t = mapChars(map, t);
			if (spec.mark) t = Array.from(t).join(spec.mark) + spec.mark;
			if (spec.join) t = Array.from(t).join(spec.join);
			if (spec.frame) t = spec.frame.replace("{text}", t);
			return t;
		},
	};
}

export const EXAMPLE = `[
	{ "name": "Sparkly bold", "base": "Bold", "frame": "✧･ﾟ: *✧･ﾟ:* {text} *:･ﾟ✧*:･ﾟ✧" },
	{ "name": "Overline", "mark": "\\u0305" },
	{ "name": "Spaced out", "base": "Wide", "join": " " },
	{ "name": "Runes", "from": "abcdefghijklmnopqrstuvwxyz", "to": "ᚨᛒᚲᛞᛖᚠᚷᚺᛁᛃᚲᛚᛗᚾᛟᛈᚲᚱᛊᛏᚢᚡᚹᛪᛃᛉ" }
]
`;

/** Your styles, or the error that kept them from loading. Missing file: none. */
export function loadCustom(path = configPath()): { styles: Style[]; error?: string } {
	let raw: string;
	try {
		raw = readFileSync(path, "utf8");
	} catch {
		return { styles: [] };
	}
	try {
		const specs = JSON.parse(raw);
		if (!Array.isArray(specs)) throw new Error("the file should hold a list: [ {...}, {...} ]");
		const styles: Style[] = [];
		// A style may build on one listed before it.
		for (const spec of specs) styles.push(customStyle(spec, [...styles, ...STYLES]));
		return { styles };
	} catch (e) {
		return { styles: [], error: `${path}: ${(e as Error).message}` };
	}
}
