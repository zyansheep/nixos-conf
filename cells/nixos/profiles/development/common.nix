{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  imports = [
    # "./aliases.nix"
    ./aliases.nix
  ];

  # Terminal
  i18n.defaultLocale = "en_US.UTF-8";
  console = {
    font = "Lat2-Terminus16";
    keyMap = "us";
  };

  # Commonly used dev packages
  environment.systemPackages = with pkgs; [
    helix # postmodern text editor
    # language servers
    vscode-langservers-extracted
    # nil
    nixd

    gnumake
    gcc
    python3
    nodejs
    gdb
    gitui # blazingly fast git ui!
  ];
}
