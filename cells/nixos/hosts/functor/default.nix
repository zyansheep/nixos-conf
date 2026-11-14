{
  inputs,
  suites,
  profiles,
  ...
}: {
  imports = with profiles; [
    suites.base
    ./hardware-configuration.nix
    ./z3code.nix # Z3 Code's server (t3code.service), kept current by z3-autoupdate

    # graphics.plasma
    graphics.drivers.amd
    graphics.sway
    fs.zfs

    core.minimal
    core.common
    core.tools
    core.communications
    core.rebuild # passwordless `sudo rebuild` / `nrb`

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
    services.binary-cache # harmonia on :5000 (tailnet), remote builds for isomorph
    services.nixos-ci # builds each branch head for the cache, the gate deploys go by; `nixos-ci status`
    services.nixos-deploy # switches to main's newest commit nixos-ci built, when idle; `nixos-deploy status`
    services.update-manager # the updates widget's agent (isomorph reports in here)
    services.tailnet-https # HTTPS for tailnet services: *.tn.zyancraft.net
    services.haveno # Haveno (Reto) Monero exchange daemon, moved from sguru
    services.z3-autoupdate # pins each new Z3 Code release on main; restarts Z3 Code after nixos-deploy switches
  ];

  # A deploy fails (and is switched back) if one of these was running before it and isn't after.
  nixos-deploy.checkUnits = ["t3code.service" "dither-chat.service" "dither-accounts.service"];

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
