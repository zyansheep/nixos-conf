import assert from "node:assert/strict";
import { test } from "node:test";
import { directionOf, tidy, translate } from "../src/lib/translate.ts";

test("direction: mostly Japanese goes to English", () => {
	assert.equal(directionOf("猫が好きです"), "ja-en");
	assert.equal(directionOf("What does 猫 mean?"), "en-ja");
	assert.equal(directionOf("I like cats"), "en-ja");
});

test("sentences from Japanese get their spaces back", () => {
	assert.equal(tidy("I like cats.It may rain.\n", "ja-en"), "I like cats. It may rain.");
	assert.equal(tidy("猫が好きです。\n", "en-ja"), "猫が好きです。");
});

const engine = process.env.JISHO_TRANSLATOR && process.env.JISHO_MODELS
	? { binary: process.env.JISHO_TRANSLATOR, models: process.env.JISHO_MODELS, state: `${process.env.HOME}/translateLocally` }
	: null;

test("the models translate both ways", { skip: !engine }, async () => {
	if (!engine) return;
	assert.match(await translate("猫が好きです。", "ja-en", engine), /cat/i);
	assert.match(await translate("I like cats.", "en-ja", engine), /猫/);
});
