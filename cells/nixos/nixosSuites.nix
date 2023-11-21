{
  inputs,
  cell,
}: let
  inherit (inputs) nixpkgs std haumea;
  l = nixpkgs.lib // builtins;
  profiles = cell.nixosProfiles;
  users = inputs.cells.home.users.nixos;
in
  {
    base = _: {
      imports = [
        # profiles.core
        users.truelecter
        users.root
      ];
    };
  }
