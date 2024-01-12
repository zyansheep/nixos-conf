{
  inputs,
  cell,
}: let
  inherit (inputs) nixpkgs std haumea;
  l = nixpkgs.lib // builtins;
  userProfiles = cell.userProfiles;
  homeModules = cell.homeModules;
  modulesImportables = l.attrValues homeModules;
in {
  nixos = rec {
    zyansheep = {pkgs, ...}: {
      home-manager.users.zyansheep = {
        imports = [userProfiles.minimal] ++ modulesImportables;

        home.stateVersion = "22.11";
      };

      programs.zsh.enable = true;

      users.users.zyansheep = {
        isNormalUser = true;
        shell = pkgs.zsh;
        extraGroups = ["wheel" "docker"];
      };
    };

    zyansheep-dev = {pkgs, ...}: {
      imports = [zyansheep];

      home-manager.users.zyansheep = {
        imports = [userProfiles.server-dev];
      };
    };

    root = {config, ...}: {
      users.users.root = {
        uid = 0;
      };
    };
  };
}
