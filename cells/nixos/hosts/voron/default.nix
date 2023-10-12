{
  inputs,
  suites,
  profiles,
  ...
}: let
  system = "aarch64-linux";
in {
  imports = [
    suites.base

    profiles.common.networking.tailscale
    profiles.faster-linux

    inputs.cells.klipper.nixosModules.klipper
    inputs.cells.secrets.nixosProfiles.wifi

    inputs.nixos-hardware.nixosModules.raspberry-pi-4

    ./_hardware-configuration.nix
    ./_wifi.nix
    ./_camera.nix
    ./_klipper
    ./_minimize.nix
  ];

  bee.system = system;
  bee.home = inputs.home-unstable;
  bee.pkgs = import inputs.latest {
    inherit system;
    config.allowUnfree = true;
    overlays = [
      inputs.cells.klipper.overlays.klipper
      (
        _: prev: {
          deviceTree.applyOverlays = prev.callPackage ./_dtmerge.nix {};
          makeModulesClosure = x: prev.makeModulesClosure (x // {allowMissing = true;});
        }
      )
    ];
  };

  networking = {
    hostName = "voron";
    firewall.enable = false;
  };

  system.stateVersion = "23.05";

  users.users.truelecter = {
    extraGroups = ["video" "gpio"];
  };

  nix.settings = {
    keep-outputs = false;
    keep-derivations = false;
    system-features = [];
  };
}
