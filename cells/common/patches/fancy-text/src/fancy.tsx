// Fancy Text: what you type, in every style at once; Enter pastes the one
// you pick into the app you came from, like the emoji picker.
import {
	Action,
	ActionPanel,
	closeMainWindow,
	Color,
	Icon,
	type LaunchProps,
	List,
	LocalStorage,
	open,
	PopToRootType,
	showToast,
	Toast,
} from "@vicinae/api";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { useEffect, useMemo, useState } from "react";
import { configPath, EXAMPLE, loadCustom } from "./custom.ts";
import { render, STYLES, type Style } from "./styles.ts";

// Set by esbuild (cells/common/packages/fancy-text.nix).
declare const FANCY_TEXT_PASTE: string;

const RECENT_KEY = "recent";
const RECENT_MAX = 8;

async function loadRecent(): Promise<string[]> {
	try {
		return JSON.parse((await LocalStorage.getItem<string>(RECENT_KEY)) ?? "[]");
	} catch {
		return [];
	}
}

async function remember(id: string) {
	const recent = (await loadRecent()).filter((r) => r !== id);
	await LocalStorage.setItem(RECENT_KEY, JSON.stringify([id, ...recent].slice(0, RECENT_MAX)));
}

// Vicinae pastes only through its input server (off here), so hand the text
// to fancy-text-paste: both selections, then Shift+Insert once the window
// we came from has focus again, as emoji-picker does.
async function paste(text: string, style: Style) {
	await remember(style.id);
	await closeMainWindow({ popToRootType: PopToRootType.Immediate });
	spawn(FANCY_TEXT_PASTE, [text], { detached: true, stdio: "ignore" }).unref();
}

// Starts the file off with an example if there isn't one yet.
async function editStyles() {
	const path = configPath();
	if (!existsSync(path)) {
		mkdirSync(dirname(path), { recursive: true });
		writeFileSync(path, EXAMPLE);
	}
	await closeMainWindow({ popToRootType: PopToRootType.Immediate });
	await open(path);
}

export default function Command(props: LaunchProps) {
	const [text, setText] = useState(props.fallbackText ?? "");
	const [seed, setSeed] = useState(() => Math.floor(Math.random() * 2 ** 32));
	const [recent, setRecent] = useState<string[]>([]);
	const custom = useMemo(() => loadCustom(), []);
	const styles = useMemo(() => [...custom.styles, ...STYLES], [custom]);

	useEffect(() => {
		loadRecent().then(setRecent);
	}, []);
	useEffect(() => {
		if (custom.error) showToast({ style: Toast.Style.Failure, title: "Your styles didn't load", message: custom.error });
	}, [custom]);

	// With nothing typed, each style shows its own name.
	const item = (style: Style, section: string) => {
		const styled = render(style, text || style.name, seed);
		return (
			<List.Item
				key={`${section}-${style.id}`}
				id={`${section}-${style.id}`}
				title={styled.replace(/\n/g, " ⏎ ")}
				accessories={[
					...(style.random ? [{ icon: { source: Icon.Shuffle, tintColor: Color.SecondaryText }, tooltip: "Random: Ctrl+R re-rolls" }] : []),
					{ text: style.name },
				]}
				actions={
					<ActionPanel>
						<Action title="Paste" icon={Icon.TextCursor} onAction={() => paste(styled, style)} />
						<Action.CopyToClipboard
							title="Copy"
							content={styled}
							shortcut={{ modifiers: ["ctrl", "shift"], key: "c" }}
							onCopy={() => remember(style.id)}
						/>
						<Action
							title="Re-Roll Random Styles"
							icon={Icon.Shuffle}
							shortcut={{ modifiers: ["ctrl"], key: "r" }}
							onAction={() => setSeed((s) => (s + 1) >>> 0)}
						/>
						<ActionPanel.Section>
							<Action title="Edit Your Styles" icon={Icon.Pencil} shortcut={{ modifiers: ["ctrl"], key: "e" }} onAction={editStyles} />
						</ActionPanel.Section>
					</ActionPanel>
				}
			/>
		);
	};

	const recentStyles = recent.map((id) => styles.find((s) => s.id === id)).filter((s): s is Style => !!s);
	return (
		<List filtering={false} searchText={text} onSearchTextChange={setText} searchBarPlaceholder="Type something to make it fancy…">
			{recentStyles.length > 0 && <List.Section title="Recent">{recentStyles.map((s) => item(s, "recent"))}</List.Section>}
			{custom.styles.length > 0 && <List.Section title="Yours">{custom.styles.map((s) => item(s, "custom"))}</List.Section>}
			<List.Section title="Styles">{STYLES.map((s) => item(s, "all"))}</List.Section>
		</List>
	);
}
