// Offline translation: translateLocally (Bergamot) running Mozilla's Firefox
// Translations models, one process per request (~0.4 s, model load included).
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { hasJapanese, isKana, isKanji } from "./text.ts";

export type Direction = "ja-en" | "en-ja";

export const DIRECTION_LABEL: Record<Direction, string> = {
	"ja-en": "日本語 → English",
	"en-ja": "English → 日本語",
};

/** Japanese to English when the text is mostly Japanese ("What does 猫 mean?" is English). */
export function directionOf(text: string): Direction {
	let japanese = 0;
	let latin = 0;
	for (const c of text) {
		if (isKana(c) || isKanji(c)) japanese++;
		else if (/[A-Za-z]/.test(c)) latin++;
	}
	return hasJapanese(text) && japanese * 3 >= latin ? "ja-en" : "en-ja";
}

export type Engine = {
	binary: string; // translateLocally.
	models: string; // An XDG data dir holding translateLocally/models/{ja-en,en-ja}-base.
	state: string; // Where translateLocally may keep its settings.
};

/** Translate the text; rejects on failure, or when the signal aborts it. */
export function translate(text: string, direction: Direction, engine: Engine, signal?: AbortSignal): Promise<string> {
	return new Promise((resolve, reject) => {
		const config = join(engine.state, "config");
		const data = join(engine.state, "data");
		mkdirSync(config, { recursive: true });
		mkdirSync(data, { recursive: true });
		const child = spawn(engine.binary, ["-m", `${direction}-base`], {
			env: {
				...process.env,
				XDG_DATA_DIRS: engine.models,
				XDG_CONFIG_HOME: config,
				XDG_DATA_HOME: data,
				QT_QPA_PLATFORM: "offscreen",
			},
			stdio: ["pipe", "pipe", "pipe"],
			signal,
		});
		let out = "";
		let err = "";
		child.stdout.setEncoding("utf8").on("data", (chunk: string) => {
			out += chunk;
		});
		child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
			err += chunk;
		});
		child.on("error", reject);
		child.on("close", (code) => {
			if (code === 0) resolve(tidy(out, direction));
			else reject(new Error(err.trim().split("\n").at(-1) || `translateLocally exited with ${code}`));
		});
		child.stdin.end(`${text.trim()}\n`);
	});
}

/** Sentences translated from Japanese come back without the spaces between them. */
export function tidy(text: string, direction: Direction): string {
	const trimmed = text.replace(/\s+$/, "");
	return direction === "ja-en" ? trimmed.replace(/([.!?])(?=["'A-Z])/g, "$1 ") : trimmed;
}
