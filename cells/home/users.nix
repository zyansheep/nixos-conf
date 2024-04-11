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
        extraGroups = ["wheel"];
      };
    };

    root = {config, ...}: {
      users.users.root = {
        uid = 0;
        initialHashedPassword = "\$6\$dnWr7aCjnJFOrj1f\$2Hb5yZCiDTvwgh.qPXSofOH/z30EHO98uwUWxBtkbrbhyXmemsl804l3LC9NX.25aaX/hl0aAIB2hcma822SX/"; # iusemutablepasswordslol
      };
    };
  };
}
