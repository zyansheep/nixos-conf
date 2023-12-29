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
    core.laptop
    gaming.common

    graphics.plasma
    graphics.drivers.intel

    # devices.xp-pen
    # devices.realtek-wifi-adapter

    services.printing
    services.syncthing
    services.containers
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
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;
  boot.loader.timeout = 1;
  boot.kernel.sysctl = {
    "dev.i915.perf_stream_paranoid" = 0;
  };

  boot.initrd.luks.devices = {
    zyandrive = {
      device = "/dev/disk/by-uuid/cf9dfcd1-68b1-41b0-8e28-f0a569f20313";
      preLVM = true;
      allowDiscards = true;
    };
  };

  networking.hostName = "isomorph";
  networking.firewall.enable = false;

  security.doas.extraRules = [{
    users = [ "zon" ];
    keepEnv = true;
    persist = true;
  }];

  /* services.udev.packages = [
    pkgs.android-udev-rules
  ]; */
  programs.adb.enable = true;
  users.users.zyansheep.extraGroups = [ "adbusers" "uucp" ];

  /* environment.systemPackages = with pkgs; [
    # arduino
  ]; */

  hardware.enableAllFirmware = true;

  programs.kdeconnect.enable = true;

  services.flatpak.enable = true;

  documentation.info.enable = false;
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
