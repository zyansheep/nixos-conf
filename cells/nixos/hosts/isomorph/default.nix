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
    ./power-conf.nix

    inputs.nixos-hardware.nixosModules.framework-13-7040-amd
    inputs.lanzaboote.nixosModules.lanzaboote

    # creative.common
    # creative.steno
    development.common
    development.tools
    development.rust
    # development.arduino
    # development.shell.starship
    # development.android

    core.minimal
    core.common
    core.tools
    # core.laptop-tlp
    # core.hacking-tools
    services.containers
    # core.privacy
    core.communications
    gaming.common
    gaming.steam

    graphics.plasma
    graphics.drivers.amd
    fs.zfs

    # devices.xp-pen
    # devices.realtek-wifi-adapter

    services.printing
    services.syncthing
    services.containers
    # services.ssh
  ];

  bee.system = system;
  bee.home = inputs.home-unstable;
  bee.pkgs = import inputs.latest {
    inherit system;
    config.allowUnfree = true;
    overlays = with inputs.cells.common.overlays; [
      common-packages
      latest-overrides
    ];
  };

  # firmware conf
  hardware = {
    enableAllFirmware = true;
    bluetooth = {
      enable = true;
      powerOnBoot = false;
    };
    # Early KMS unnecessarily slows boot
    amdgpu.loadInInitrd = false;
    pulseaudio.enable = false; # use pipewire
    sensor.iio.enable = false;
  };

  # Bootloader
  boot = {
    zfs = {
      extraPools = ["zpool"];
    };
    loader.efi.canTouchEfiVariables = true;
    lanzaboote = {
      enable = true;
      pkiBundle = "/persist/etc/secureboot";
    };
    # Erase my Darlings https://grahamc.com/blog/erase-your-darlings/
    # Note don't do this until you've added links to persist dataset for passwords, github login, fingerprint, timezone
    initrd.postDeviceCommands = lib.mkAfter ''
      zfs rollback -r zpool/local/root@blank
    '';
    # Enable kernel lockdown (note this prevents hibernation from working, but it doesn't work anyway because of ZFS). Note: Takes too long to build
    /* kernelPatches = [{
     name = "kernel-lockdown";
     patch = null;
     extraConfig = ''
       SECURITY_LOCKDOWN_LSM y
       MODULE_SIG y
     '';
    }]; */
  };

  environment.etc = {
    "shadow" = {source = "/persist/etc/shadow";}; # persist passwords
    "NetworkManager/system-connections" = {source = "/persist/etc/NetworkManager/system-connections/";}; # persist NetworkManager
  };
  systemd.tmpfiles.rules = [
    "L /var/lib/bluetooth - - - - /persist/var/lib/bluetooth" # persist bluetooth connections
    "L /var/lib/fprint - - - - /persist/var/lib/fprint" # persist fingerprints
  ];
  time.timeZone = lib.mkDefault "America/New_York";

  boot.kernelParams = [
    "zfs.zfs_arc_max=12884901888" # Set Adaptive Replacement Cache size to max 12gb. (machine-specific)
    # https://community.frame.work/t/12th-gen-not-sending-xf86monbrightnessup-down/20605/11
    "module_blacklist=hid_sensor_hub" # Q: What is the difference between this and boot.blacklistedKernelModules?
    "rtc_cmos.use_acpi_alarm=1" # Fix system wake-up after 5 minutes sleep for suspend-them-hibernate (I don't hibernate, is this causing my issue?)
    "usbcore.autosuspend=20"
  ];
  boot.extraModprobeConfig = ''
    options cfg80211 ieee80211_regdom="US" # configure regulatory domain
    options snd_hda_intel power_save=1 # sound card powersave
  '';

  services.earlyoom.enable = false; # ZFS does not mark pages as cache and thus will trigger earlyoom even when plenty of memory available.

  networking.hostId = "14df389e";
  networking.hostName = "isomorph";
  networking.firewall.enable = false;

  # doas
  security.doas.extraRules = [
    {
      users = ["zyansheep"];
      keepEnv = true;
      persist = true;
    }
  ];

  # groups
  users.users.zyansheep.extraGroups = ["adbusers" "uucp"];

  documentation.info.enable = false;

  # Services
  services.fwupd.enable = true;
  services.fprintd.enable = true;
  programs.kdeconnect.enable = true;
  programs.nix-ld.enable = true;
  services.flatpak.enable = true;

  # services.plantuml-server.enable = true;

  # nix.sandboxPaths = [ "/bin/sh=${pkgs.bash}/bin/sh" ];
  # nix.useSandbox = false;

  # This value determines the NixOS release from which the default
  # settings for stateful data, like file locations and database versions
  # on your system were taken. It‘s perfectly fine and recommended to leave
  # this value at the release version of the first install of this system.
  # Before changing this value read the documentation for this option
  # (e.g. man configuration.nix or on https://nixos.org/nixos/options.html).
  system.stateVersion = "24.05"; # Did you read the comment?
}
