{
  inputs,
  suites,
  profiles,
  lib,
  ...
}: let
  system = "x86_64-linux";
in {
  imports = with profiles; [
    suites.base

    ./hardware-configuration.nix
    inputs.nixos-hardware.nixosModules.framework-13-7040-amd
    inputs.lanzaboote.nixosModules.lanzaboote

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

  # enable all firmware
  hardware.enableAllFirmware = true;

  # Bootloader
  boot.loader.grub = {
    zfsSupport = true;
    efiSupport = true;
    efiInstallAsRemovable = true;
    mirroredBoots = [ { devices = ["nodev"]; path = "/boot"; } ];
  };

  # Secureboot config
  /* boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.lanzaboote = {
    enable = true;
    pkiBundle = "/etc/secureboot";
  }; */
  
  # ZFS
  boot.zfs.extraPools = [ "zpool" ];
  
  networking.hostId = "14df389e";
  networking.hostName = "isomorph";
  networking.firewall.enable = false;

  # doas
  security.doas.extraRules = [{
    users = [ "zyansheep" ];
    keepEnv = true;
    persist = true;
  }];

  # groups
  users.users.zyansheep.extraGroups = [ "adbusers" "uucp" ];

  programs.kdeconnect.enable = true;
  services.flatpak.enable = true;

  documentation.info.enable = false;
  services.fwupd.enable = true;

  # Power Management
  services.power-profiles-daemon.enable = false;
  services.tlp = {
    enable = true;
    settings = {
      CPU_DRIVER_OPMODE_ON_AC = "active";
      CPU_DRIVER_OPMODE_ON_BAT = "active";

      CPU_SCALING_GOVERNOR_ON_AC = "powersave";
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";

      CPU_ENERGY_PERF_POLICY_ON_AC = "balance_performance";
      CPU_ENERGY_PERF_POLICY_ON_BAT = "balance_power";

      START_CHARGE_THRESH_BAT0 = 70;
      STOP_CHARGE_THRESH_BAT0 = 80;
    };
  };

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
