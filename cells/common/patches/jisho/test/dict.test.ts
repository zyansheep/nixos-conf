// Against the real database (JISHO_DB, set by the package's checkPhase).
import assert from "node:assert/strict";
import { test } from "node:test";
import { Dictionary } from "../src/lib/dict.ts";
import { entryMarkdown, kanjiMarkdown, pitchHtml } from "../src/lib/render.ts";

const path = process.env.JISHO_DB;
const d = path ? new Dictionary(path) : null;
const top = (q: string, n = 1) => (d as Dictionary).search(q, n).map((h) => h.entry.head);

test("English finds the word you mean first", { skip: !d }, () => {
	assert.deepEqual(top("eat"), ["食べる"]);
	assert.deepEqual(top("dog"), ["犬"]);
	assert.deepEqual(top("water"), ["水"]);
	assert.deepEqual(top("head"), ["頭"]);
	assert.deepEqual(top("rice ball"), ["おにぎり"]);
});

test("Japanese, inflected Japanese and romaji", { skip: !d }, () => {
	assert.deepEqual(top("ねこ"), ["猫"]);
	assert.deepEqual(top("neko"), ["猫"]);
	assert.deepEqual(top("taberu"), ["食べる"]);
	const hit = (d as Dictionary).search("食べました", 1)[0];
	assert.equal(hit.entry.head, "食べる");
	assert.equal(hit.inflection, "polite past");
	assert.deepEqual(top("行ってきます"), ["行ってきます"]); // The phrase itself before 行く.
	assert.deepEqual(top("きた", 2), ["北", "来る"]);
});

test("sentences split into their words", { skip: !d }, () => {
	const words = (d as Dictionary).segment("猫が好きです。").filter((s) => s.hits.length).map((s) => s.hits[0].entry.head);
	assert.deepEqual(words, ["猫", "が", "好き", "です"]);
	const inflected = (d as Dictionary).segment("昨日は寿司を食べました");
	assert.equal(inflected.find((s) => s.text === "食べました")?.hits[0].entry.head, "食べる");
});

test("JLPT levels, frequency and pitch come with the words", { skip: !d }, () => {
	const eat = (d as Dictionary).search("食べる", 1)[0].entry;
	assert.equal(eat.jlpt, 5);
	assert.equal(eat.common, true);
	assert.ok(eat.freq && eat.freq < 1000);
	assert.deepEqual(eat.pa?.["たべる"], [2]);
	assert.equal((d as Dictionary).search("経済", 1)[0].entry.jlpt, 4);
});

test("kanji: data, meanings and words written with them", { skip: !d }, () => {
	const k = (d as Dictionary).kanji("食");
	assert.ok(k?.m.includes("eat") && k.sc === 9 && k.svg?.startsWith("<svg"));
	assert.deepEqual((d as Dictionary).kanjiMeaning("water", 1).map((x) => x.c), ["水"]);
	assert.ok((d as Dictionary).wordsWith("食", 5).some((e) => e.head === "食べる"));
});

test("detail pages render", { skip: !d }, () => {
	const hit = (d as Dictionary).search("食べました", 1)[0];
	const md = entryMarkdown(hit, d as Dictionary);
	assert.match(md, /polite past form of 食べる/);
	assert.match(md, /もっと果物を<b>食べる<\/b>べきです/);
	assert.match(kanjiMarkdown((d as Dictionary).kanji("食") as never, d as Dictionary), /data:image\/svg\+xml;base64,/);
});

test("pitch: overlines over the high morae, ꜜ at the drop", () => {
	const o = (s: string) => `<span style="text-decoration:overline">${s}</span>`;
	assert.equal(pitchHtml("たべる", 2), `た${o("べ")}ꜜる`);
	assert.equal(pitchHtml("さくら", 0), `さ${o("くら")}`);
	assert.equal(pitchHtml("はし", 1), `${o("は")}ꜜし`);
});
