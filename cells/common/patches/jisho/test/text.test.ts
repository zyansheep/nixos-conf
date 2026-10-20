import assert from "node:assert/strict";
import { test } from "node:test";
import { englishKey, fold, kanaToRomaji, morae, romajiToKana } from "../src/lib/text.ts";

test("queries fold as the database does", () => {
	assert.equal(fold("ネコ"), "ねこ");
	assert.equal(fold("ﾈｺ"), "ねこ"); // Half-width katakana.
	assert.equal(fold("ＴＥＳＴ"), "test");
	assert.equal(fold("食べる"), "食べる");
});

test("English keys drop case, parentheses (nested too) and to/a/the", () => {
	assert.equal(englishKey("to eat (something)"), "eat");
	assert.equal(englishKey("dog (Canis (lupus) familiaris)"), "dog");
	assert.equal(englishKey("The Sky."), "sky");
	assert.equal(englishKey("rice ball"), "rice ball");
});

test("romaji as an IME takes it", () => {
	assert.equal(romajiToKana("taberu"), "たべる");
	assert.equal(romajiToKana("konnichiwa"), "こんにちわ");
	assert.equal(romajiToKana("kanpai"), "かんぱい");
	assert.equal(romajiToKana("gakkou"), "がっこう");
	assert.equal(romajiToKana("matcha"), "まっちゃ");
	assert.equal(romajiToKana("shinbun"), "しんぶん");
	assert.equal(romajiToKana("kon'ya"), "こんや");
	assert.equal(romajiToKana("tsukue"), "つくえ");
	assert.equal(romajiToKana("kyou"), "きょう");
	assert.equal(romajiToKana("ra-men"), "らーめん");
	assert.equal(romajiToKana("eat"), null); // Not romaji: "t" alone.
	assert.equal(romajiToKana("xyz"), null);
});

test("kana as romaji, as you'd type it", () => {
	assert.equal(kanaToRomaji("たべる"), "taberu");
	assert.equal(kanaToRomaji("とうきょう"), "toukyou");
	assert.equal(kanaToRomaji("がっこう"), "gakkou");
	assert.equal(kanaToRomaji("まっちゃ"), "matcha");
	assert.equal(kanaToRomaji("こんや"), "kon'ya");
	assert.equal(kanaToRomaji("ラーメン"), "raamen");
	assert.equal(kanaToRomaji("しゃしん"), "shashin");
});

test("morae: small kana join the one before", () => {
	assert.deepEqual(morae("きょう"), ["きょ", "う"]);
	assert.deepEqual(morae("がっこう"), ["が", "っ", "こ", "う"]);
});
