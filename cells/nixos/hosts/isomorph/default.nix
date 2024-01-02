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
    inputs.nixos-hardware.nixosModules.framework-13-7040-amd

    # creative.common
    # creative.steno
    development.common
    # development.shell.zsh
    # development.arduino
    # development.shell.starship

    development.rust
    # development.android

    core.minimal
    core.common
    core.tools
    # core.hacking-tools
    services.containers
    # core.privacy
    core.communications
    core.laptop-tlp
    gaming.common
    gaming.steam

    graphics.plasma
    # graphics.drivers.intel

    # devices.xp-pen
    # devices.realtek-wifi-adapter

    services.printing
    services.syncthing
    services.containers
    services.ssh
  ];

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
  boot.loader.grub = {
    enable = true;
    zfsSupport = true;
    efiSupport = true;
    efiInstallAsRemovable = true;
    mirroredBoots = [ { devices = ["nodev"]; path = "/boot"; } ];
  };
  
  boot.zfs.extraPools = [ "zpool" ];
  
  networking.hostId = "14df389e";
  networking.hostName = "isomorph";
  networking.firewall.enable = false;

  security.doas.extraRules = [{
    users = [ "zon" ];
    keepEnv = true;
    persist = true;
  }];

  users.users.zyansheep.extraGroups = [ "adbusers" "uucp" ];

  hardware.enableAllFirmware = true;

  programs.kdeconnect.enable = true;

  services.flatpak.enable = true;

  documentation.info.enable = false;
  services.fwupd.enable = true;

  # nix.sandboxPaths = [ "/bin/sh=${pkgs.bash}/bin/sh" ];
  # nix.useSandbox = false;

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "22.05"; # Did you read the comment?
}
