{ inputs, common }: { lib, config, pkgs, ... }:
with lib;
{
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

  # Development Packages
  environment.systemPackages = with pkgs; [
    gnumake
    gcc
    python3
    vscodium
    gdb

    /* (vscode-with-extensions.override {
      vscode = vscodium;
      vscodeExtensions = with vscode-extensions; [
        bbenoist.nix
        llvm-vs-code-extensions.vscode-clangd
        vadimcn.vscode-lldb
        mkhl.direnv
      ];
    }) */
    nodejs # So I don't have to constantly do nix-shell -p nodejs
  ];
}
