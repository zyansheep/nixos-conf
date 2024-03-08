{
  inputs,
  suites,
  profiles,
  ...
}: let
  system = "x86_64-linux";
in {
  imports = with profiles; [
    suites.base
    ./hardware-configuration.nix
    # devices.webcam-loopback
    # devices.rtl-sdr

    graphics.plasma
    fs.zfs

    core.minimal
    core.common
    core.tools
    core.privacy
    core.communications

    gaming.common
    gaming.steam
    # creative.common

    development.common
    development.rust
    # development.android
    # development.shell.zsh
    # development.arduino

    services.printing
    services.ssh
    services.syncthing
    services.containers
  ];

  nix.settings.auto-optimise-store = true;

  bee.system = system;
  bee.home = inputs.home;
  bee.pkgs = import inputs.nixos {
    inherit system;
    config.allowUnfree = true;
    overlays = with inputs.cells.common.overlays; [
      common-packages
      latest-overrides
    ];
  };

  # Bootloader
  boot = {
    zfs = {extraPools = ["zpool"];};
    loader.systemd-boot.enable = true;
  };

  # Hostname
  networking.hostName = "functor";
  networking.hostId = "95196fe2";
  networking.firewall.enable = false;

  security.doas.extraRules = [
    {
      users = ["zyansheep"];
      keepEnv = true;
      persist = true;
    }
  ];

  services.flatpak.enable = true;

  # Enable virtualization
  virtualisation.libvirtd.enable = true;

  system.stateVersion = "23.05"; # Did you read the comment?
}
