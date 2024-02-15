{
  inputs,
  cell,
}: let
  inherit (inputs) nixpkgs std haumea;
  l = nixpkgs.lib // builtins;
  suites = with cell.homeProfiles; {
    base = [
      shell.direnv
      git
      dev.codium
      shell.zsh
      shell.nvim
      home-manager-base
      ssh
    ];
    develop = [
      dev.aws
      dev.terraform
      dev.nix
    ];
    android = [dev.android];
  };
in {
  workstation = {...}: {
    imports = with suites;
      l.flatten [
        base
        develop
        develop-gui
        android
      ];
  };
  minimal = {...}: {imports = suites.base;};
  server-dev = {...}: {
    imports = with suites;
      l.flatten [
        develop
        inputs.nixos-vscode-server.homeModules.default
      ];
  };
}
