{
  inputs,
  suites,
  profiles,
  ...
}: {
  imports = with profiles; [
    suites.base
    ./hardware-configuration.nix

    core.minimal
    # core.common
    # core.tools

    # devices.xp-pen
    # devices.realtek-wifi-adapter
    services.syncthing
    services.containers
    services.ssh
  ];

  # firmware conf
  hardware.enableAllFirmware = true;

  # Bootloader
  boot.loader.grub = {
    zfsSupport = true;
    efiSupport = true;
    efiInstallAsRemovable = true;
    mirroredBoots = [
      {
        devices = ["nodev"];
        path = "/boot";
      }
    ];
  };

  # ZFS
  boot.zfs = {
    extraPools = ["mpool"];
  };
  services.zfs.autoScrub.enable = true; # Auto scrub every sunday at 2am
  boot.kernelParams = ["zfs.zfs_arc_max=12884901888"]; # Set Adaptive Replacement Cache size to max 12gb.
  services.earlyoom.enable = false; # ZFS does not mark pages as cache and thus will trigger earlyoom even when plenty of memory available.

  networking.hostId = "49408a0f";
  networking.firewall.enable = false;

  # doas
  security.doas.extraRules = [
    {
      keepEnv = true;
      persist = true;
    }
  ];

  # groups

  documentation.info.enable = false;

  # Services
  # services.flatpak.enable = true;

  # nix.sandboxPaths = [ "/bin/sh=${pkgs.bash}/bin/sh" ];
  # nix.useSandbox = false;

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It's perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "24.05"; # Did you read the comment?
}
