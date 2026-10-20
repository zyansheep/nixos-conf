import assert from "node:assert/strict";
import { test } from "node:test";
import { classesOf, conjugationTable, deinflect, describe } from "../src/lib/inflect.ts";

/** The reasons the text deinflects to base by, if it does (with the class). */
function reasonsFor(text: string, base: string, cls: string): string | undefined {
	const found = deinflect(text).find((d) => d.text === base && (d.types === null || d.types.includes(cls)));
	return found && describe(found.reasons);
}

test("common forms deinflect to their dictionary form", () => {
	assert.equal(reasonsFor("食べました", "食べる", "v1"), "polite past");
	assert.equal(reasonsFor("食べさせられなかった", "食べる", "v1"), "causative · potential or passive · negative · past");
	assert.equal(reasonsFor("書かされた", "書く", "v5k"), "causative passive · past");
	assert.equal(reasonsFor("行ってきます", "行く", "v5k-s"), "~てくる · polite");
	assert.equal(reasonsFor("勉強しています", "勉強する", "vs-i"), "progressive (~ている) · polite");
	assert.equal(reasonsFor("高くなかった", "高い", "adj-i"), "negative · past");
	assert.equal(reasonsFor("来なかった", "来る", "vk"), "negative · past");
	assert.equal(reasonsFor("こない", "くる", "vk"), "negative");
	assert.equal(reasonsFor("読んでしまった", "読む", "v5m"), "completed (~てしまう) · past");
	assert.equal(reasonsFor("飲みたくない", "飲む", "v5m"), "want to (~たい) · negative");
	assert.equal(reasonsFor("泳げる", "泳ぐ", "v5g"), "potential");
});

test("a chain only runs through real forms", () => {
	assert.equal(reasonsFor("食べた", "食べる", "v5r"), undefined); // た is ichidan's past, not godan's.
	assert.equal(reasonsFor("書いた", "書う", "v5u"), undefined); // う-verbs end った.
});

test("every form in a conjugation table deinflects back", () => {
	const words: [string, string][] = [
		["食べる", "v1"], ["書く", "v5k"], ["泳ぐ", "v5g"], ["話す", "v5s"], ["待つ", "v5t"], ["死ぬ", "v5n"],
		["遊ぶ", "v5b"], ["読む", "v5m"], ["帰る", "v5r"], ["買う", "v5u"], ["行く", "v5k-s"], ["する", "vs-i"],
		["来る", "vk"], ["くる", "vk"], ["高い", "adj-i"],
	];
	for (const [word, cls] of words) {
		const table = conjugationTable(word, [cls]);
		assert.ok(table, word);
		for (const [form, ...cells] of table) {
			for (const cell of cells) {
				if (cell === "—" || cell === word || /まい$|な$|です$/.test(cell)) continue;
				if (cell.startsWith("でき")) continue; // する's potential is its own verb.
				assert.ok(reasonsFor(cell, word, classesOf([cls])[0]) !== undefined, `${form} ${cell} of ${word}`);
			}
		}
	}
});

test("conjugation tables", () => {
	const table = Object.fromEntries((conjugationTable("書く", ["v5k"]) ?? []).map(([form, yes, no]) => [form, [yes, no]]));
	assert.deepEqual(table["Polite"], ["書きます", "書きません"]);
	assert.deepEqual(table["Te-form"], ["書いて", "書かなくて"]);
	assert.deepEqual(table["Potential"], ["書ける", "書けない"]);
	assert.deepEqual(table["Volitional"], ["書こう", "書くまい"]);
	const aru = Object.fromEntries((conjugationTable("ある", ["v5r-i"]) ?? []).map(([form, yes, no]) => [form, [yes, no]]));
	assert.deepEqual(aru["Past"], ["あった", "なかった"]);
	const ii = Object.fromEntries((conjugationTable("いい", ["adj-ix"]) ?? []).map(([form, yes, no]) => [form, [yes, no]]));
	assert.deepEqual(ii["Past"], ["よかった", "よくなかった"]);
	const study = Object.fromEntries((conjugationTable("勉強", ["n", "vs"]) ?? []).map(([form, yes, no]) => [form, [yes, no]]));
	assert.deepEqual(study["Polite"], ["勉強します", "勉強しません"]);
	assert.equal(conjugationTable("猫", ["n"]), null);
});
