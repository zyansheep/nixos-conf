import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { customStyle, EXAMPLE, loadCustom } from "../src/custom.ts";
import { render, STYLES } from "../src/styles.ts";

const style = (id: string) => STYLES.find((s) => s.id === id)!;

test("every style renders, ids are unique", () => {
	assert.equal(new Set(STYLES.map((s) => s.id)).size, STYLES.length);
	for (const s of STYLES) {
		const out = render(s, "Hello, World! 123 :) <3", 7);
		assert.ok(out.length > 0, s.name);
		assert.ok(!out.includes("undefined"), s.name);
	}
});

test("character maps, as lingojam has them", () => {
	assert.equal(render(style("cursive"), "Hello", 0), "𝓗𝓮𝓵𝓵𝓸");
	assert.equal(render(style("doubleStruck"), "Hi 42", 0), "ℍ𝕚 𝟜𝟚");
	assert.equal(render(style("oldEnglish"), "Hello", 0), "ℌ𝔢𝔩𝔩𝔬");
	assert.equal(render(style("tiny"), "Hello", 0), "ʜᴇʟʟᴏ");
	assert.equal(render(style("flip"), "Hello", 0), "ollǝH");
	assert.equal(render(style("bubbles"), "Hi", 0), "Ⓗⓘ");
	assert.equal(render(style("strike"), "ab", 0), "a̶b̶");
	assert.equal(render(style("starWave"), "hi", 0), "★彡 hi 彡★");
});

test("works on code points, so astral input survives", () => {
	assert.equal(render(style("hearts"), "a😀b", 0), "a♥😀♥b");
	assert.equal(render(style("flip"), "a😀", 0), "😀ɐ");
});

test("random styles hold still for a seed and move with it", () => {
	for (const id of ["crazy", "cute", "creepy", "vaporwave"]) {
		assert.equal(render(style(id), "Hello world", 3), render(style(id), "Hello world", 3), id);
	}
	const rolls = new Set(Array.from({ length: 8 }, (_, seed) => render(style("crazy"), "Hello world", seed)));
	assert.ok(rolls.size > 1);
});

test("your styles: the example loads and each step applies", () => {
	const dir = mkdtempSync(join(tmpdir(), "fancy-text-"));
	writeFileSync(join(dir, "styles.json"), EXAMPLE);
	const { styles, error } = loadCustom(join(dir, "styles.json"));
	assert.equal(error, undefined);
	assert.deepEqual(styles.map((s) => s.name), ["Sparkly bold", "Overline", "Spaced out", "Runes"]);
	assert.equal(render(styles[0], "hi", 0), "✧･ﾟ: *✧･ﾟ:* 𝐡𝐢 *:･ﾟ✧*:･ﾟ✧");
	assert.equal(render(styles[1], "ab", 0), "a̅b̅");
	assert.equal(render(styles[2], "ab", 0), "ａ ｂ");
	assert.equal(render(customStyle({ name: "x", from: "ab", to: "😀b" }), "abc", 0), "😀bc");
});

test("your styles: mistakes come back as one message", () => {
	const dir = mkdtempSync(join(tmpdir(), "fancy-text-"));
	const load = (json: string) => (writeFileSync(join(dir, "s.json"), json), loadCustom(join(dir, "s.json")));
	assert.match(load("{").error!, /JSON/);
	assert.match(load('[{"name":"a","base":"nope"}]').error!, /no style called "nope"/);
	assert.match(load('[{"name":"a","from":"ab","to":"x"}]').error!, /2 characters but "to" has 1/);
	assert.match(load('[{"name":"a","frame":"**"}]').error!, /\{text\}/);
	assert.deepEqual(loadCustom(join(dir, "missing.json")), { styles: [] });
});
