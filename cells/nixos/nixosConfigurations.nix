{
  inputs,
  cell,
}: let
  inherit (inputs) nixpkgs;
  lib = nixpkgs.lib // builtins;
  cells = inputs.cells;
in
  cells.common.lib.importSystemConfigurations {
    src = ./hosts;

    inherit inputs lib;
    suites = cell.nixosSuites;
    profiles =
      cell.nixosProfiles
      // {
        common = cells.common.commonProfiles;
        users = cells.home.users.nixos;
      };
    userProfiles = cells.home.userProfiles;
  }
