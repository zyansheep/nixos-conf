{
  inputs,
  suites,
  profiles,
  ...
}: {
  imports = with profiles; [
    suites.base
    ./hardware-configuration.nix
    # devices.webcam-loopback
    # devices.rtl-sdr

    # graphics.plasma
    graphics.drivers.amd
    graphics.sway
    fs.zfs

    core.minimal
    core.common
    core.tools
    core.communications

    gaming.common
    gaming.steam
    # creative.common

    development.common
    development.rust
    development.tools
    # development.android
    # development.shell.zsh
    # development.arduino

    services.printing
    # services.ssh
    services.syncthing
    services.containers
  ];

  # Bootloader
  boot = {
    zfs = {extraPools = ["zpool"];};
    loader.systemd-boot.enable = true;
  };

  # Hostname
  networking.hostId = "95196fe2";
  networking.firewall.enable = false;

  security.doas.extraRules = [
    {
      users = ["zyansheep"];
      keepEnv = true;
      persist = true;
    }
  ];

  # enable firefox nightly
  # programs.firefox.package =
  # inputs.firefox.packages.${system}.firefox-nightly-bin;
  networking.networkmanager.wifi.backend = "wpa_supplicant";

  services.flatpak.enable = true;
  services.mullvad-vpn.enable = true;

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

  # Enable virtualization
  virtualisation.libvirtd.enable = true;

  system.stateVersion = "23.05"; # Did you read the comment?
}
