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

## Where to look

| Location | Contents |
| --- | --- |
| [flake.nix](flake.nix) | Inputs and available host configurations |
| [cells/nixos/hosts](cells/nixos/hosts) | Host-specific settings and hardware |
| [cells/nixos/profiles](cells/nixos/profiles) | Reusable system and service configuration |
| [cells/home](cells/home) | Home Manager configuration |
| [dotfiles/.config](dotfiles/.config) | Desktop and application configuration |
| [cells/common](cells/common) | Local packages, patches and pinned sources |

For desktop controls and customization, see the [Waybar guide](dotfiles/.config/waybar/README.md).
For Wi-Fi, VPNs, saved connections, or migration from the old networking setup,
see [isomorph networking](cells/nixos/hosts/isomorph/README.md).
