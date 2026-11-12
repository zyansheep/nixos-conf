# For agents working in this repo

## Test anything with a window in a background compositor, always

This repo configures the desktop its owner is sitting at (Waybar, niri, the panels under the bar). Never test on the
live session: no pointer moves, clicks, typing, workspace switches or test windows on the real screen. On the real
session do only the install itself (file edits, a build, restarting the one service you changed) and at most a plain
`grim` screenshot, which needs no input.

A screenshot of something drawn is not a test of it. Before saying a button, a key or a link works, press it with
real input in the background compositor and look at what it did.

- **Looking:** `WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman sway -c <config>` with a private
  `XDG_RUNTIME_DIR` (a temp dir, mode 700) and `output HEADLESS-1 resolution 2820x1880 scale 2` (isomorph is
  1410 x 940 logical), then `grim` on that display. `wtype` keys reach it.
- **Clicking and typing:** a pure headless sway has no pointer, so `swaymsg seat … cursor press` and `wlrctl pointer`
  do nothing. Run `sway-unwrapped` on its X11 backend (`WLR_BACKENDS=x11`, output `X11-1`) inside a private `Xvfb`
  and drive it with `xdotool` on that X server only (X pixels are logical pixels times the scale).
- sway is not on this system: get it, `xvfb` and `xdotool` with
  `nix build --inputs-from . nixpkgs#<name> --no-link --print-out-paths`, never by changing a profile.
- Keep the private `XDG_RUNTIME_DIR` path short (a Wayland socket path over 108 characters fails). In a sandboxed
  agent shell `dbus-run-session` has no config and `/etc/localtime` is UTC: start `dbus-daemon` with a config file of
  your own that has a `<listen>` address, and set `TZ` for what you start.
- A program started in the test display must not take the real session's D-Bus name (use its standalone flag or a
  private bus), and must not open the real browser: give it a stand-in for `xdg-open`. A real browser may run there
  only with `--no-remote` and a throwaway profile.
- sway takes keyboard focus straight back from an on-demand layer surface, where niri leaves it, so a panel that
  closes on focus loss closes at once there: use the panel's keep-open flag for the rest of the checks.
- Sizes measured there are not the real ones (fonts differ); positions relative to the screen's middle are.
- Stop what you started by the PIDs you recorded, not by matching command lines: a pattern on a store path once
  killed the real session's copy of the same program.
- Restarting Waybar kills anything started from it that still lives in its unit (a browser once did). Check
  `systemd-cgls --user-unit waybar.service` first; to reload its config without a restart, send SIGUSR2 to its main
  PID only, never `systemctl kill`.

## Applying changes: ask with `nrb-request`, never sudo

Agents don't switch the system themselves (and can't: T3 Code starts them with NoNewPrivileges, so
sudo fails). Ask the user instead:

```sh
nrb-request "REASON" [rebuild args...]   # = update-manager ask-rebuild; e.g. --override-input foo path:/x
```

- It stages `~/nixos-conf` (`git add .`, like `nrb`), builds the system without root and shows the user a
  full-screen window: REASON, the closure diff, the commits since the running system, the uncommitted
  files, a message box, Deny and Approve. On Approve it runs `sudo rebuild ARGS`. It rebuilds
  `~/nixos-conf`, not a worktree: get the change there first.
- Write REASON for the user: what the switch applies and why, in a sentence or two.
- It blocks until the user answers (up to 15 minutes, plus the build and the switch): run it in the
  background (Claude: `run_in_background: true`, outside the Bash sandbox, since it starts a job under
  the user's systemd manager).
- Exit status: 0 switched, or nothing to switch; 77 denied; 75 no answer; 69 no window could be shown;
  1 the build or the switch failed. The job's output streams to stdout, and the user's message comes
  last ("The user's message: ..."): read it and do what it says. If the command was interrupted, the
  request goes on: `update-manager job list`, `update-manager job log ID`.
- One switch or request at a time: a second one waits for the first.
- Don't route around a denial or a missing answer (`update-manager do rebuild`, the switch units,
  `systemd-run --user sudo rebuild`, ...): the decision is the user's, even where the system can't
  enforce it.
