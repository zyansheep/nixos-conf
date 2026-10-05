{
  inputs,
  suites,
  profiles,
  ...
}: {
  imports = with profiles; [
    suites.base
    ./hardware-configuration.nix

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

    development.common
    development.rust
    development.tools
    # development.android
    # development.shell.zsh

    services.printing
    services.ssh
    services.tailscale
    services.syncthing
    services.containers
    services.zfs-snapshots
  ];

  # Bootloader
  boot = {
    zfs = {extraPools = ["zpool"];};
    loader.systemd-boot.enable = true;
  };

  # Hostname
  networking.hostId = "95196fe2";
  networking.firewall.enable = false;

  networking.networkmanager.wifi.backend = "wpa_supplicant";

  services.flatpak.enable = true;

  # Enable virtualization
  virtualisation.libvirtd.enable = true;

  system.stateVersion = "23.05"; # Did you read the comment?
}
