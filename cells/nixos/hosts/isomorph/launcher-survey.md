# Unified launcher survey

Investigated 2026-09-16. This is a proposal; the launcher binding is unchanged.

## Current configuration

`dotfiles/.config/niri/config.kdl:433` binds Mod+D to
`rofi -run-use-desktop-cache -show run`. Installed Rofi reports 2.0.0.
This selects executable-name search, rather than the desktop-application mode
(`drun`). It starts Rofi on each invocation. The configured cache-looking flag
should not be treated as evidence that a desktop application index is being used.
No startup latency benchmark was performed, so the exact cause of the reported
delay is still unproven.

Walker 2.17.0 is already installed through the home packages profile, but is not
bound to Mod+D. No Elephant command was found in the inspected PATH.
Mod+Shift+D currently runs the separate Nix package installer, and Mod+C uses
Rofi for clipboard history.

## Recommendation

Use an existing launcher. Prefer **Walker + Elephant** for this desktop's GTK
styling and an explicitly combined search across independent providers. Prefer
**Vicinae** if minimizing integration code matters more than keeping the same UI
framework. Prototype the two finalists before replacing Mod+D permanently.

### Candidates

| Project | Existing capabilities | Remaining work / tradeoff |
| --- | --- | --- |
| [Walker](https://github.com/abenz1267/walker) + [Elephant](https://github.com/abenz1267/elephant) | GTK4/Rust frontend; resident service; independent Go provider backend over a Unix socket; applications, PATH commands, files, web search, bookmarks, windows, custom menus | Best styling fit. No dedicated live-tab or Steam provider verified in the upstream provider tree; add adapters. |
| [Vicinae](https://docs.vicinae.com/) | Native Qt interface; file search, apps, browser tabs, windows; script commands and React/TypeScript extensions; partial Raycast compatibility | Strongest ready-made feature set. Different UI toolkit. Verify how tab/game extension results participate in root search versus opening a separate command. |
| [Anyrun](https://github.com/anyrun-org/anyrun) | Wayland launcher with Rust plugins for desktop applications, shell commands, Kidex file search, web search and Niri focus | Viable smaller framework, but more integration work for this exact set of sources. |
| [Albert](https://albertlauncher.github.io/basics/) | Qt plugin architecture; global handlers combine results and usage-based ranking; [C++/Python extension APIs](https://albertlauncher.github.io/extension/) | Good general-purpose alternative; live Floorp integration and Niri focus behavior need validation. |
| [Rofi](https://github.com/davatorium/rofi/blob/next/doc/rofi.1.markdown) | Already installed; run, drun, file browser, combined modes and script modes | Lowest migration cost, but a cached multi-source backend would still be our responsibility. |

The Walker README documents keeping the UI alive with
`walker --gapplication-service`, and a Unix-socket activation path. Its GTK CSS
and per-provider item layouts can match the existing menus without adopting
libadwaita window chrome. Elephant is a separate resident service, so providers
can refresh without blocking display of the launcher.

Vicinae has [declarative Nix/Home Manager integration](https://docs.vicinae.com/nixos),
including a user service and file results in root search. Its current
[CMake configuration](https://github.com/vicinaehq/vicinae/blob/main/CMakeLists.txt)
uses Qt6; TypeScript extensions render native UI rather than a browser page.
Raycast compatibility is partial, so finding a Raycast extension is not proof
that it works on this machine.

## Coverage of the requested sources

- **GUI applications:** desktop entries from the XDG data directories, including
  Nix profiles and Flatpak exports. Search display name, executable and keywords.
- **Programs:** independently index PATH executables. Offer “run” and “run in
  terminal”; desktop files remain the preferred way to launch GUI apps correctly.
- **Steam games:** desktop shortcuts work immediately where present, but do not
  guarantee complete library coverage. A small provider can read Steam's local
  library metadata and installed-game manifests, deduplicate by app ID, and
  launch via Steam. Searching all owned but uninstalled games is an additional
  requirement, potentially needing authenticated/API data; it should not delay
  installed-game results. No dedicated supported provider was verified in the
  inspected Walker/Elephant and Vicinae extension trees.
- **Open Floorp pages:** search live tab titles and URLs, then focus the existing
  tab and its browser window. This is different from opening a URL in a new tab.
  Vicinae already implements a [browser extension/native host bridge](https://github.com/vicinaehq/vicinae/tree/main/src/browser-extension).
  Its [host installer](https://github.com/vicinaehq/vicinae/blob/main/src/server/src/services/browser-extension/native-host-installer.cpp)
  explicitly recognizes Floorp. A [Firefox add-on listing](https://addons.mozilla.org/en-US/firefox/addon/vicinae/)
  exists, although the repository README still says unpublished: verify addon ID,
  maintainer and protocol compatibility rather than relying on that stale sentence.
  Niri foreground activation, multiple profiles and Floorp workspaces still need
  a live acceptance test.
- **Files:** begin with filename/path search in explicitly configured directories.
  Elephant's [files provider](https://github.com/abenz1267/elephant/tree/master/internal/providers/files)
  uses `fd` for indexing and supports previews. Vicinae includes a dedicated
  [file indexer](https://github.com/vicinaehq/vicinae/tree/main/src/file-indexer).
  Neither should be advertised here as full document-content search. That can
  be a separate opt-in provider later.
- **New URLs / web searches:** explicit actions opening Floorp; transmit a search
  only on activation, not every time the user types a local query.

Tab search does **not** need OS process IDs. A normal extension with tabs and
native-messaging permissions can list and activate tabs using the public
[WebExtensions API](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/tabs/update).
It therefore does not require the AutoConfig privilege setting used by our
CPU/memory attribution bridge. That existing snapshot contains process-level
labels, not stable tab IDs or a tab-activation channel, so it cannot substitute
for a proper tab integration.

## Proposed Walker implementation

1. Add a Home Manager launcher profile with pinned, compatible Walker/Elephant
   packages and graphical-session user services. Pass the correct PATH and XDG
   environment. Keep the UI resident; Mod+D only sends an activation request.
2. Configure application, runner, file and web providers. Use shared colors,
   typography, spacing and corner radii from the desktop menus.
3. Add an installed-Steam-games adapter, initially as a cached custom menu or
   provider. Watch the small metadata files instead of rescanning game folders.
4. Add a Firefox-compatible extension/native host and a Floorp-tabs provider.
   Send incremental tab-created/updated/removed events; activate using profile,
   window and tab IDs. Exclude private windows by default. Use typed requests
   and argv-based execution, never shell interpolation of titles or URLs.
5. Combine relevant results by default, with optional prefixes such as `app`,
   `>`, `game`, `tab`, and `/`. Deduplicate Steam shortcuts against game entries.
   Weight exact matches and recent/frequent use; cap each source so PATH binaries
   or files cannot overwhelm applications and tabs. Give slow providers a deadline
   and discard responses to obsolete queries.
6. Keep file indexing restricted to selected directories. Exclude build outputs,
   caches, `.git`, dependencies and Steam game contents unless explicitly wanted.
   Update incrementally and open from cached results without a full rescan.

### Validation before switching

Measure actual keypress-to-first-frame time (not merely the CLI's exit time),
first-result latency, idle CPU/RSS and incremental indexing cost. Suggested goals:
warm opening under 100 ms and useful cached results under 150 ms; these are
acceptance targets, not measured promises. Test new app installs, terminal
commands, multiple Steam libraries, file rename/removal, browser reconnect,
multiple Floorp windows/profiles and Niri focus behavior. Verify Unicode input,
Escape, click-away dismissal and scrollable results. Pilot on an alternate
shortcut, then replace Mod+D after these checks pass.

A new launcher from scratch is unnecessary. The likely custom work is limited
to source adapters and ranking/configuration, with the existing project handling
window creation, keyboard navigation, rendering and theme support.
