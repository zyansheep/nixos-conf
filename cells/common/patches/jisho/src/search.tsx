// Japanese Dictionary: one search bar for English, Japanese (inflected or not)
// and romaji, kanji, and offline translation of anything sentence-like.
import {
	Action,
	ActionPanel,
	Color,
	Detail,
	environment,
	type LaunchProps,
	List,
	LocalStorage,
} from "@vicinae/api";
import { join } from "node:path";
import { useEffect, useMemo, useRef, useState } from "react";
import { Dictionary, type Entry, type Hit, type Kanji, type Segment } from "./lib/dict.ts";
import {
	conjugationMarkdown,
	entryMarkdown,
	jlptLabel,
	kanjiJlpt,
	kanjiMarkdown,
	readingsOf,
	summary,
	translationMarkdown,
} from "./lib/render.ts";
import { DIRECTION_LABEL, type Direction, directionOf, type Engine, translate } from "./lib/translate.ts";
import { englishKey, fold, hasJapanese } from "./lib/text.ts";

// Set by esbuild (cells/common/packages/jisho.nix).
declare const JISHO_TRANSLATOR: string;
declare const JISHO_MODELS: string;

let dictionary: Dictionary | undefined;
function dict(): Dictionary {
	dictionary ??= new Dictionary(join(environment.assetsPath, "jisho.sqlite3"));
	return dictionary;
}

const engine = (): Engine => ({
	binary: JISHO_TRANSLATOR,
	models: JISHO_MODELS,
	state: join(environment.supportPath, "translateLocally"),
});

const JLPT_COLORS: Record<number, Color> = {
	5: Color.Green,
	4: Color.Blue,
	3: Color.Yellow,
	2: Color.Orange,
	1: Color.Red,
};

// Words nobody looks up in an English sentence.
const STOP_WORDS = new Set(
	"i me my you your he him his she her it its we us our they them their a an the to of and or in on at by for with from is are am was were be been do does did will would can could should shall may might must not no this that these those there here so if as".split(
		" ",
	),
);

type Results = {
	words: Hit[];
	sentence: Segment[];
	kanji: Kanji[];
	/** Where the translation row goes: first for sentences, last otherwise. */
	translation: "first" | "last" | null;
};

const NO_RESULTS: Results = { words: [], sentence: [], kanji: [], translation: null };

function lookup(text: string): Results {
	const q = text.trim();
	if (!q) return NO_RESULTS;
	const d = dict();
	const length = [...q].length;
	if (hasJapanese(q)) {
		const words = d.japanese(q, 30);
		const whole = words.some(
			(h) => h.inflection || fold(h.entry.head) === fold(q) || h.entry.r.some((r) => fold(r[0]) === fold(q)) || h.entry.k.some((k) => fold(k[0]) === fold(q)),
		);
		const sentenceLike = length >= 4 && (!whole || /[。、！？!?\s]/.test(q));
		const sentence = sentenceLike ? d.segment(q).filter((s) => s.hits.length) : [];
		return {
			words: sentence.length > 1 ? words.slice(0, 8) : words,
			sentence: sentence.length > 1 ? sentence : [],
			kanji: d.kanjiIn(q).slice(0, 12),
			translation: sentenceLike ? "first" : length >= 2 ? "last" : null,
		};
	}
	const tokens = q.split(/\s+/);
	const sentenceLike = tokens.length >= 3 || (tokens.length >= 2 && /[.?!]$/.test(q));
	const sentence: Segment[] = [];
	if (sentenceLike) {
		const seen = new Set<number>();
		for (const token of tokens) {
			const word = englishKey(token);
			if (!word || STOP_WORDS.has(word)) continue;
			const hit = d.english(word, 1)[0];
			if (hit && !seen.has(hit.entry.id)) {
				seen.add(hit.entry.id);
				sentence.push({ text: word, hits: [hit] });
			}
		}
	}
	return {
		words: d.search(q, sentenceLike ? 10 : 30),
		sentence,
		kanji: tokens.length <= 2 ? d.kanjiMeaning(q, 3) : [],
		translation: sentenceLike ? "first" : q.length >= 2 ? "last" : null,
	};
}

// ---- Recent lookups -------------------------------------------------------

const RECENT_KEY = "recent";
const RECENT_MAX = 30;

async function loadRecent(): Promise<number[]> {
	try {
		return JSON.parse((await LocalStorage.getItem<string>(RECENT_KEY)) ?? "[]");
	} catch {
		return [];
	}
}

async function remember(id: number) {
	const recent = (await loadRecent()).filter((x) => x !== id);
	await LocalStorage.setItem(RECENT_KEY, JSON.stringify([id, ...recent].slice(0, RECENT_MAX)));
}

// ---- Translation ----------------------------------------------------------

type Translation = { key: string; value?: string; error?: string };
const translations = new Map<string, Translation>();

function useTranslation(text: string | null, direction: Direction): Translation | null {
	const key = text ? `${direction}\u0000${text}` : "";
	const [state, setState] = useState<Translation | null>(null);
	useEffect(() => {
		if (!text) return;
		const cached = translations.get(key);
		if (cached) {
			setState(cached);
			return;
		}
		setState({ key });
		const controller = new AbortController();
		// Typing: wait for a pause before starting translateLocally.
		const timer = setTimeout(() => {
			translate(text, direction, engine(), controller.signal).then(
				(value) => {
					const done = { key, value };
					translations.set(key, done);
					setState(done);
				},
				(error: Error) => {
					if (!controller.signal.aborted) setState({ key, error: error.message });
				},
			);
		}, 250);
		return () => {
			clearTimeout(timer);
			controller.abort();
		};
	}, [key]);
	return text ? (state?.key === key ? state : { key }) : null;
}

// ---- Views ----------------------------------------------------------------

export default function Command(props: LaunchProps) {
	return <Search initial={props.fallbackText ?? ""} />;
}

function Search({ initial }: { initial: string }) {
	// Vicinae resets the selection on the first render after a search event,
	// whenever that comes; so every event renders, a repeat of the text too (the
	// launch sends one), or the reset would land on a later, unrelated render: a
	// translation arriving would jump the list back to the top.
	const [{ text }, setSearch] = useState({ text: initial, events: 0 });
	// Translate when the translation row (last, for words) is selected; flip on request.
	const [wanted, setWanted] = useState(false);
	const [flipped, setFlipped] = useState(false);
	const onSearchTextChange = (value: string) => {
		setSearch((s) => ({ text: value, events: s.events + 1 }));
		setWanted(false);
		setFlipped(false);
	};
	const [recent, setRecent] = useState<Entry[]>([]);
	// The first render only shows the window, so it's quick: Vicinae unloads a
	// command that isn't up within its handshake deadline.
	const [started, setStarted] = useState(false);
	useEffect(() => setStarted(true), []);
	const results = useMemo(() => (started ? lookup(text) : NO_RESULTS), [started, text]);
	const q = text.trim();
	const natural = directionOf(q);
	const direction: Direction = flipped ? (natural === "ja-en" ? "en-ja" : "ja-en") : natural;
	const translating = results.translation === "first" || wanted;
	const translation = useTranslation(results.translation && translating ? q : null, direction);

	useEffect(() => {
		loadRecent().then((ids) => setRecent(ids.map((id) => dict().entry(id)).filter((e): e is Entry => !!e)));
	}, []);

	// Remember a word once it has been looked at for a moment. (Moving the
	// selection mustn't render: only the translation row's first selection does.)
	const dwell = useRef<ReturnType<typeof setTimeout>>(undefined);
	const onSelectionChange = (id: string | null) => {
		if (id === "translate" && !wanted) setWanted(true);
		clearTimeout(dwell.current);
		const entryId = id?.match(/^(?:w|s|r)-(\d+)/)?.[1];
		if (entryId) dwell.current = setTimeout(() => remember(Number(entryId)), 1500);
	};
	useEffect(() => () => clearTimeout(dwell.current), []);

	const translationItem = results.translation && q && (
		<List.Section title="Translation" key="translation">
			<List.Item
				id="translate"
				title={translation?.value?.replace(/\s+/g, " ") ?? (translation?.error ? "Translation failed" : translating ? "Translating…" : `Translate “${q}”`)}
				subtitle={DIRECTION_LABEL[direction]}
				icon="🌐"
				detail={<List.Item.Detail markdown={translationMarkdown(q, translation?.value ?? null, `${DIRECTION_LABEL[direction]} · offline`, translation?.error, results.sentence)} />}
				actions={
					<ActionPanel>
						{translation?.value && <Action.CopyToClipboard title="Copy Translation" content={translation.value} />}
						<Action.CopyToClipboard title="Copy Original" content={q} shortcut={{ modifiers: ["ctrl", "shift"], key: "c" }} />
						<Action
							title={`Translate ${DIRECTION_LABEL[direction === "ja-en" ? "en-ja" : "ja-en"]}`}
							shortcut={{ modifiers: ["ctrl"], key: "s" }}
							onAction={() => setFlipped(!flipped)}
						/>
						<ActionPanel.Section title="Online">
							<Action.OpenInBrowser
								title="Open in Google Translate"
								url={`https://translate.google.com/?sl=${direction.slice(0, 2)}&tl=${direction.slice(3)}&op=translate&text=${encodeURIComponent(q)}`}
								shortcut={{ modifiers: ["ctrl"], key: "g" }}
							/>
							<Action.OpenInBrowser
								title="Open in DeepL"
								url={`https://www.deepl.com/translator#${direction.slice(0, 2)}/${direction.slice(3)}/${encodeURIComponent(q)}`}
								shortcut={{ modifiers: ["ctrl"], key: "d" }}
							/>
						</ActionPanel.Section>
					</ActionPanel>
				}
			/>
		</List.Section>
	);

	// A lone kanji is most likely asked about as a kanji.
	const kanjiFirst = [...q].length === 1 && results.kanji.length === 1;
	const kanjiSection = results.kanji.length > 0 && (
		<List.Section title="Kanji" key="kanji">
			{results.kanji.map((k) => (
				<KanjiItem key={k.c} kanji={k} />
			))}
		</List.Section>
	);

	const empty = !q;
	const nothing = started && !empty && !results.words.length && !results.sentence.length && !results.kanji.length && !results.translation;
	return (
		<List
			isShowingDetail={!empty || recent.length > 0}
			filtering={false}
			searchText={text}
			onSearchTextChange={onSearchTextChange}
			onSelectionChange={onSelectionChange}
			isLoading={!!translation && !translation.value && !translation.error}
			searchBarPlaceholder="English, 日本語, romaji, or a sentence to translate…"
		>
			{empty && !recent.length && (
				<List.EmptyView
					icon="🇯🇵"
					title="Japanese ↔ English"
					description={"Words in English, Japanese or romaji (taberu); conjugated ones too (食べました).\nPaste a sentence to translate it, offline.\n\nJMdict & KANJIDIC (EDRDG), KanjiVG, Tatoeba, JPDB, kanjium · CC BY-SA"}
				/>
			)}
			{nothing && <List.EmptyView icon="🔍" title="No matches" description={`Nothing in the dictionary for “${q}”.`} />}
			{empty && recent.length > 0 && (
				<List.Section title="Recent">
					{recent.map((entry) => (
						<WordItem key={entry.id} id={`r-${entry.id}`} hit={{ entry, score: 0, matched: entry.head }} />
					))}
				</List.Section>
			)}
			{kanjiFirst && kanjiSection}
			{results.translation === "first" && translationItem}
			{results.sentence.length > 0 && (
				<List.Section title="Words in this sentence">
					{results.sentence.map((segment, i) => (
						<WordItem key={`s-${i}`} id={`s-${segment.hits[0].entry.id}-${i}`} hit={segment.hits[0]} surface={segment.text} />
					))}
				</List.Section>
			)}
			{results.words.length > 0 && (
				<List.Section title={results.sentence.length ? "Dictionary" : "Words"} subtitle={`${results.words.length}`}>
					{results.words.map((hit) => (
						<WordItem key={hit.entry.id} id={`w-${hit.entry.id}`} hit={hit} />
					))}
				</List.Section>
			)}
			{!kanjiFirst && kanjiSection}
			{results.translation === "last" && translationItem}
		</List>
	);
}

function accessories(hit: Hit): List.Item.Accessory[] {
	const out: List.Item.Accessory[] = [];
	if (hit.inflection) out.push({ text: { value: hit.inflection, color: Color.SecondaryText } });
	if (hit.entry.jlpt) out.push({ tag: { value: jlptLabel(hit.entry.jlpt), color: JLPT_COLORS[hit.entry.jlpt] } });
	else if (hit.entry.common) out.push({ tag: { value: "common", color: Color.SecondaryText } });
	return out;
}

function WordItem({ id, hit, surface }: { id: string; hit: Hit; surface?: string }) {
	const { entry } = hit;
	const markdown = useMemo(() => entryMarkdown(hit, dict()), [hit]);
	const reading = entry.reading === entry.head ? "" : entry.reading;
	const title = surface && fold(surface) !== fold(entry.head) ? `${surface} → ${entry.head}` : entry.head;
	return (
		<List.Item
			id={id}
			title={title}
			subtitle={`${reading ? `${reading}  ` : ""}${summary(entry)}`}
			accessories={accessories(hit)}
			detail={<List.Item.Detail markdown={markdown} />}
			actions={<WordActions entry={entry} />}
		/>
	);
}

function WordActions({ entry }: { entry: Entry }) {
	const conjugations = conjugationMarkdown(entry, dict().tags);
	const kanji = dict().kanjiIn(entry.head);
	const refs = [...new Set(entry.s.flatMap((s) => [...(s.rel ?? []), ...(s.ant ?? [])].map(([w]) => w)))].slice(0, 6);
	const meaning = entry.s.map((s, i) => `${i + 1}. ${s.g.join("; ")}`).join("\n");
	const reading = readingsOf(entry).join("・");
	return (
		<ActionPanel>
			<ActionPanel.Section>
				<Action.CopyToClipboard title="Copy Word" content={entry.head} onCopy={() => remember(entry.id)} />
				<Action.CopyToClipboard
					title="Copy Reading"
					content={entry.reading}
					shortcut={{ modifiers: ["ctrl"], key: "r" }}
					onCopy={() => remember(entry.id)}
				/>
				<Action.CopyToClipboard
					title="Copy Word, Reading and Meaning"
					content={`${entry.head}${entry.head === reading ? "" : `（${reading}）`}\n${meaning}`}
					shortcut={{ modifiers: ["ctrl", "shift"], key: "c" }}
					onCopy={() => remember(entry.id)}
				/>
			</ActionPanel.Section>
			<ActionPanel.Section>
				{conjugations && (
					<Action.Push
						title="Show Conjugations"
						shortcut={{ modifiers: ["ctrl"], key: "t" }}
						target={<Detail navigationTitle={`${entry.head} conjugations`} markdown={conjugations} />}
					/>
				)}
				{kanji.length > 0 && (
					<Action.Push
						title={`Show Kanji ${kanji.map((k) => k.c).join("")}`}
						shortcut={{ modifiers: ["ctrl", "shift"], key: "k" }}
						target={<KanjiList kanji={kanji} />}
					/>
				)}
				{refs.map((ref) => (
					<Action.Push key={ref} title={`Look Up ${ref}`} target={<Search initial={ref} />} />
				))}
			</ActionPanel.Section>
			<ActionPanel.Section title="Online">
				<Action.OpenInBrowser
					title="Open on Jisho.org"
					url={`https://jisho.org/search/${encodeURIComponent(entry.head)}`}
					shortcut={{ modifiers: ["ctrl"], key: "o" }}
				/>
				<Action.OpenInBrowser
					title="Example Sentences on Tatoeba"
					url={`https://tatoeba.org/en/sentences/search?from=jpn&to=eng&query=${encodeURIComponent(entry.head)}`}
				/>
			</ActionPanel.Section>
		</ActionPanel>
	);
}

function KanjiItem({ kanji }: { kanji: Kanji }) {
	const markdown = useMemo(() => kanjiMarkdown(kanji, dict()), [kanji]);
	const level = kanjiJlpt(kanji.jl);
	return (
		<List.Item
			id={`k-${kanji.c}`}
			title={kanji.c}
			subtitle={kanji.m.slice(0, 4).join(", ")}
			accessories={[
				...(kanji.sc ? [{ text: { value: `${kanji.sc}画`, color: Color.SecondaryText } }] : []),
				...(level ? [{ tag: { value: level, color: Color.SecondaryText } }] : []),
			]}
			detail={<List.Item.Detail markdown={markdown} />}
			actions={
				<ActionPanel>
					<Action.CopyToClipboard title="Copy Kanji" content={kanji.c} />
					<Action.CopyToClipboard title="Copy Meanings" content={kanji.m.join(", ")} shortcut={{ modifiers: ["ctrl", "shift"], key: "c" }} />
					<Action.Push
						title={`Words with ${kanji.c}`}
						shortcut={{ modifiers: ["ctrl"], key: "w" }}
						target={<WordsWith kanji={kanji} />}
					/>
					<Action.OpenInBrowser
						title="Open on Jisho.org"
						url={`https://jisho.org/search/${encodeURIComponent(`${kanji.c} #kanji`)}`}
						shortcut={{ modifiers: ["ctrl"], key: "o" }}
					/>
				</ActionPanel>
			}
		/>
	);
}

function KanjiList({ kanji }: { kanji: Kanji[] }) {
	return (
		<List isShowingDetail navigationTitle={`Kanji ${kanji.map((k) => k.c).join("")}`}>
			{kanji.map((k) => (
				<KanjiItem key={k.c} kanji={k} />
			))}
		</List>
	);
}

function WordsWith({ kanji }: { kanji: Kanji }) {
	const words = useMemo(() => dict().wordsWith(kanji.c, 60), [kanji]);
	return (
		<List isShowingDetail navigationTitle={`Words with ${kanji.c}`} searchBarPlaceholder={`Filter words with ${kanji.c}…`}>
			{words.map((entry) => (
				<WordItem key={entry.id} id={`w-${entry.id}`} hit={{ entry, score: 0, matched: entry.head }} />
			))}
		</List>
	);
}
