# Nix Configuration

Zyan's NixOS and Home Manager configuration, adapted from truelector (thank you!).
The `isomorph` desktop uses Niri and Waybar.

Keep this checkout at `~/nixos-conf`: the desktop dotfiles are linked directly
from it, and the rebuild wrapper uses that location.

## Rebuild

Run from the checkout on a configured machine:

```sh
nrb   # stage changes, rebuild and switch the current host
nrbu  # update flake inputs, then run nrb
```

`nrb` stages all changes with `git add .`; it does not commit them. Without the
aliases, the switch command for `isomorph` is:

```sh
sudo nixos-rebuild switch --flake .#isomorph
```

Add new files to Git before rebuilding so the flake includes them.

Coding agents don't run `nrb`; they run `nrb-request "REASON" [rebuild args...]`
(`update-manager ask-rebuild`). It builds the system without root and opens a
full-screen window with the reason, what changes, a message box, Deny and
Approve. Approve runs `nrb` with those args; the agent gets the output, your
message and the exit status (77 denied, 75 no answer within 15 minutes). See
[AGENTS.md](AGENTS.md) and update-manager's README.

## Where to look

| Location | Contents |
| --- | --- |
| [flake.nix](flake.nix) | Inputs and available host configurations |
| [cells/nixos/hosts](cells/nixos/hosts) | Host-specific settings and hardware |
| [cells/nixos/profiles](cells/nixos/profiles) | Reusable system and service configuration |
| [cells/home](cells/home) | Home Manager configuration |
| [dotfiles/.config](dotfiles/.config) | Desktop and application configuration |
| [cells/common](cells/common) | Local packages and patches |

For desktop controls and customization, see the [Waybar guide](dotfiles/.config/waybar/README.md).
For the launcher, Wi-Fi, VPNs, saved connections and Flatpak troubleshooting,
see [the isomorph guide](cells/nixos/hosts/isomorph/README.md).
