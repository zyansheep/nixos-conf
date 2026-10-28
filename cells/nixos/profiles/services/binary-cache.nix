# Binary cache + remote builder host (functor). Clients: services.functor-cache.
#
# - harmonia serves this machine's /nix/store on :5000, tailnet only (systemd
#   IP filter on the socket; the firewall is off fleet-wide). It signs on the
#   fly with /var/lib/nix-cache/functor-1.sec, a root-only file that is never
#   in the repo or the store. Clients trust the public half:
#     functor-1:i1pZhmrkClUdYof/2PuSWV2bRl9IS6A6DDR01ZIKKdM=
#   If the key is lost, generate a new one and replace the public key in
#   services/functor-cache.nix:
#     nix key generate-secret --key-name functor-2 > /var/lib/nix-cache/functor-2.sec
#   Note that whatever lands in this store gets served signed, so trusting
#   this cache means trusting everyone who can write the store here (root and
#   trusted-users: @wheel, nixremote).
# - nixremote: ssh user for remote builds. Its one key may only speak the nix
#   daemon protocol. It has to be a trusted user (remote builds hand over
#   unsigned input-addressed derivations), which makes it root-equivalent;
#   the key belongs to root on isomorph.
# - All nix builds here (CI, remote builds, local rebuilds) run in
#   nix-builds.slice at a low CPU weight, so they use idle cores but yield to
#   the desktop. Nice= on a client does nothing: builds are children of
#   nix-daemon, not of whoever asked for them.
{
  inputs,
  common,
}: {pkgs, ...}: {
  services.harmonia.cache = {
    enable = true;
    # A string, not a path literal: a path would copy the key into the store.
    signKeyPaths = ["/var/lib/nix-cache/functor-1.sec"];
    settings.priority = 50; # below cache.nixos.org (40), which stays first
  };
  systemd.tmpfiles.rules = ["d /var/lib/nix-cache 0700 root root -"];
  # Socket-activated: the socket unit owns the listening fd, so the IP filter
  # goes here. Tailscale's v4 CGNAT and v6 ULA ranges, plus localhost.
  systemd.sockets.harmonia.socketConfig = {
    IPAddressDeny = "any";
    IPAddressAllow = ["localhost" "100.64.0.0/10" "fd7a:115c:a1e0::/48"];
  };

  users.users.nixremote = {
    isSystemUser = true;
    group = "nixremote";
    shell = pkgs.bashInteractive; # sshd runs the forced command through it
    openssh.authorizedKeys.keys = [
      ''command="/run/current-system/sw/bin/nix-daemon --stdio",restrict ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOqCFSC1TwQGqu3DjSEQW9h3o8Q6+M4UZb+XJ+kAdzQN root@isomorph (nix remote builds on functor)''
    ];
  };
  users.groups.nixremote = {};
  nix.settings.trusted-users = ["nixremote"];

  # CPUWeight is relative to system.slice and user.slice (100 each): under
  # contention builds get ~1/6 of the CPU, otherwise all of it. ZFS does its
  # own I/O scheduling, so IOWeight/ioprio would do nothing here.
  systemd.slices.nix-builds = {
    description = "Nix builds (low priority)";
    sliceConfig.CPUWeight = 20;
  };
  systemd.services.nix-daemon.serviceConfig = {
    Slice = "nix-builds.slice";
    # Under memory pressure the OOM killer takes a build, not the desktop.
    OOMScoreAdjust = 500;
  };
  nix.daemonCPUSchedPolicy = "batch";
}
