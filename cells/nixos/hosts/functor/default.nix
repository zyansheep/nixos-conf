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
    services.z3-autoupdate # pins each new Z3 Code release on main; restarts Z3 Code once no thread runs
    services.input-bump # moves followed flake inputs (japanese-weave) to their newest commit; `input-bump status`
    services.dither-approvals # dither-ask, the approval keys; the review watcher and merge bot for nixos-conf
    services.cliproxyapi # CLIProxyAPI on 127.0.0.1:8317: pooled Claude subscriptions for Claude Code
    services.sguru-backup # nightly restic backups of sguru's Vaultwarden and headscale, pulled over ssh
  ];

  # A deploy fails (and is switched back) if one of these was running before it and isn't after.
  nixos-deploy.checkUnits = ["t3code.service" "dither-chat.service" "dither-accounts.service" "japanese-weave-collector.service"];

  # review/<name> branches of nixos-conf are merged by the bot once the user approves on their phone or laptop.
  dither-approvals.review.enable = true;

  # Japanese Weave: a merge to its main is bumped here once its checks pass, then deployed like any commit.
  input-bump.inputs = [
    {
      name = "japanese-weave";
      url = "https://github.com/zyansheep/japanese-weave.git";
    }
  ];
  # The collector: the one home of the word state and the gloss log, on the tailnet (cells/home/profiles/japanese-weave.nix
  # has the client side). The unit files `fw collector install` wrote by hand are taken over.
  home-manager.users.zyansheep = {
    services.japanese-weave.collector = {
      enable = true;
      listen = "100.64.0.4:7853";
    };
    xdg.configFile = {
      "systemd/user/japanese-weave-collector.service".force = true;
      "systemd/user/default.target.wants/japanese-weave-collector.service".force = true;
    };
  };

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

  # No swap otherwise (the disk is all ZFS, and swap on a zvol can deadlock). zram gives idle browser pages
  # somewhere to go, and earlyoom only kills once memory AND swap are both under 10%, so ZFS's ARC (counted
  # as used, not available) no longer sets it off on its own.
  zramSwap.enable = true;

  # KSM: the kernel merges identical memory pages. ~40 account browsers (dither-accounts) run copies of the same
  # Chromium; their processes opt in (MemoryKSM=yes on the dither-accounts unit, inherited by every extractor), so
  # only those pages are scanned.
  hardware.ksm.enable = true;

  # Enable virtualization
  virtualisation.libvirtd.enable = true;

  system.stateVersion = "23.05"; # Did you read the comment?
}
