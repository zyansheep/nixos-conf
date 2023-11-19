{
  inputs,
  cell,
}: let
  profiles = cell.nixosProfiles;
  nixosProfiles = inputs.cells.nixos.nixosProfiles;
  users = inputs.cells.home.users.nixos;
in {
  base = _: {
    imports = [
      nixosProfiles.core
      users.truelecter
      profiles.root-user
    ];
  };
  tailscale = _: {
    imports = [
      profiles.tailscale
    ];
  };
}
