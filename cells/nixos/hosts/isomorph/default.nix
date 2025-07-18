{ inputs, suites, profiles, ... }:
let system = "x86_64-linux";
in {
  imports = with profiles; [
    suites.base

    ./hardware-configuration.nix
    # ./power-conf.nix

    inputs.nixos-hardware.nixosModules.framework-13-7040-amd
    inputs.lanzaboote.nixosModules.lanzaboote
    inputs.impermanence.nixosModules.impermanence

    # creative.common
    # creative.steno
    development.common
    development.tools
    development.rust
    development.r-lang
    # development.arduino
    # development.shell.starship
    development.android

    core.minimal
    core.common
    core.tools
    # core.laptop-tlp
    # core.hacking-tools
    # core.privacy
    core.communications
    gaming.common
    gaming.steam

    #graphics.plasma
    graphics.sway
    graphics.drivers.amd # enable graphics drivers for AMD cpu
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
    # amdgpu.loadInInitrd = false;
    # pulseaudio.enable = false; # use pipewire
    # sensor.iio.enable = false;
  };
  zramSwap.enable = true;

  # networking.networkmanager.wifi.backend = "wpa_supplicant";
  networking.networkmanager.enable = false;
  networking.wireless.iwd.enable = true;
  networking.wireless.iwd.settings = {
    Network = {
      # EnableIPv6 = true;
      RoutePriorityOffset = 300;
    };
    Settings = { AutoConnect = true; };
  };

  # Bootloader
  boot = {
    zfs = { extraPools = [ "zpool" ]; };
    loader.efi.canTouchEfiVariables = true;
    lanzaboote = {
      enable = true;
      pkiBundle = "/persist/etc/secureboot";
    };
  };

  # Root Rollback via "Erase my Darlings" https://grahamc.com/blog/erase-your-darlings/
  environment.persistence."/persist" = {
    directories = [
      "/var/log" # for journalctl
      "/var/lib/bluetooth"
      "/var/lib/fprint"
      "/var/lib/systemd/coredump"
      "/var/lib/iwd"
      # "/etc/NetworkManager/system-connections"
      "/etc/mullvad-vpn"
      "/var/lib/waydroid" # persist Waydroid data
    ];
    files = [ "/etc/machine-id" ];
  };
  zfsConfig.enableSystemdRollback = true;
  environment.etc = { "shadow".source = "/persist/etc/shadow"; };
  boot.kernelParams = [
    "zfs.zfs_arc_max=12884901888" # Set Adaptive Replacement Cache size to max 12gb. (machine-specific)
    # https://community.frame.work/t/12th-gen-not-sending-xf86monbrightnessup-down/20605/11
    # "module_blacklist=hid_sensor_hub" # Q: What is the difference between this and boot.blacklistedKernelModules?
    # "rtc_cmos.use_acpi_alarm=1" # Fix system wake-up after 5 minutes sleep for suspend-them-hibernate (I don't hibernate, is this causing my issue?)
    # "usbcore.autosuspend=20"
  ];
  boot.extraModprobeConfig = ''
    options cfg80211 ieee80211_regdom="US" # configure regulatory domain
  '';

  services.earlyoom.enable =
    false; # ZFS does not mark pages as cache and thus will trigger earlyoom even when plenty of memory available.

  networking.hostId = "14df389e";
  networking.hostName = "isomorph";
  networking.firewall.enable = false;

  # groups
  users.users.zyansheep.extraGroups = [ "adbusers" "uucp" "waydroid" ];

  documentation.info.enable = false;

  # Services
  services.blueman.enable = true;
  services.fwupd.enable = true; # firmware update
  services.fprintd.enable = true; # fingerprint
  # programs.kdeconnect.enable = true; # kde connect
  # services.paperless.enable = true; # paperless doc ocr
  # services.paperless.settings = {
  #   PAPERLESS_ADMIN_USER = "admin";
  #   PAPERLESS_ADMIN_PASSWORD = "password";
  #   PAPERLESS_AUTO_LOGIN_USERNAME = "admin";
  # };
  # programs.firejail.enable = true;
  # Firefox Nightly
  # programs.firefox.package = inputs.firefox.packages.${system}.firefox-nightly-bin;
  programs.firefox.enable = false;
  services.flatpak.enable = true; # enable flatpak
  services.mullvad-vpn.enable = true;
  
  # Waydroid configuration
  virtualisation.waydroid.enable = true;
  # services.logmein-hamachi.enable = true; # enable logmein
  # Nix Helper
  /* programs.nh = {
       enable = true;
       clean.enable = true;
       clean.extraArgs = "--keep-since 4d --keep 3";
       flake = "/home/zyansheep/nixos-conf";
     };
  */

  # enable command-not-found
  /* environment.etc."programs.sqlite".source =
    inputs.flake-programs-sqlite.packages.${system}.programs-sqlite;
  programs.command-not-found.enable = true;
  programs.command-not-found.dbPath = "/etc/programs.sqlite"; */

  home-manager.users.zyansheep.programs.command-not-found.enable = true;

  # zfs snapshot service
  services.sanoid = {
    enable = true;
    interval = "hourly";

    datasets = {
      "zpool/safe" = {
        hourly = 1;
        daily = 15;
        monthly = 12;
        yearly = 1;
        autoprune = true;
        autosnap = true;
        recursive = true;
      };
    };
  };

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
