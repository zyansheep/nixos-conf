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
