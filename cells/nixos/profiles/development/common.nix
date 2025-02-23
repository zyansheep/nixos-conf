{ inputs, common, }:
{ lib, config, pkgs, ... }:
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
    vscode-langservers-extracted # json, jsonc, other servers...
    ruff # python
    # nil
    nixd

    gnumake
    gcc
    (pkgs.writeShellScriptBin "python" ''
      export LD_LIBRARY_PATH=$NIX_LD_LIBRARY_PATH
      exec ${pkgs.python3}/bin/python "$@"
    '')
    nodejs
    gdb
    gitui # blazingly fast git ui!
  ];
}
