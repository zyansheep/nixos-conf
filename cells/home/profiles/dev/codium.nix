_: {
  pkgs,
  lib,
  ...
}: {
  # programs.vscodium, not programs.vscode with the vscodium package: that one
  # writes VS Code's paths (~/.vscode, Code/User) and warns on every rebuild.
  # Settings and keybindings stay unmanaged (~/.config/VSCodium/User).
  programs.vscodium = {
    enable = true;
    package = pkgs.vscodium; # Uses vscodium from overlay (latest)
  };
}
