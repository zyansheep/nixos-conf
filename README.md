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

Coding agents can't use `sudo` (T3 Code starts them with NoNewPrivileges). On
isomorph they run `nrb-request "REASON" [rebuild args...]` instead: it builds the
system without root and shows a notification with the reason, what changes and
Approve / Deny. Approve runs `nrb` with those args; the agent gets the output and
exit status (77 denied, 75 no answer within 15 minutes). The output of the
last request is in `journalctl --user -u nrb-approve`. See [AGENTS.md](AGENTS.md)
and [rebuild.nix](cells/nixos/profiles/core/rebuild.nix).

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
