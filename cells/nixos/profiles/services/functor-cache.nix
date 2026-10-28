# Use functor (services.binary-cache) as a binary cache and remote builder.
# Its CI (services.nixos-ci) builds origin/main, so patched packages that
# cache.nixos.org doesn't have (vicinae, waybar, ...) download instead of
# compiling here; unpushed changes compile on functor via the remote builder.
#
# - Cache: `rebuild` (core.rebuild) adds it only when functor answers within
#   2 s. In nix.conf it would cost ~20 s on any download while functor is
#   unreachable (Nix retries a dead cache 5 times before skipping it).
#   cache.nixos.org (priority 40) is still asked first; functor is 50.
# - Builder: when functor is unreachable, a build loses one ssh
#   ConnectTimeout and then builds here as before.
#
# One-time setup: root's key for nixremote@functor, outside the repo and the
# store, at /persist/etc/nix/functor-builder_ed25519 (root, 0600). Its public
# half is authorized in services/binary-cache.nix.
{
  inputs,
  common,
}: _: let
  functor = "100.64.0.4"; # tailnet (headscale) address
in {
  nix.settings = {
    trusted-public-keys = ["functor-1:i1pZhmrkClUdYof/2PuSWV2bRl9IS6A6DDR01ZIKKdM="];
    # A miss is remembered for an hour by default, so a path functor's CI
    # finished a minute after a rebuild asked would still compile here.
    narinfo-cache-negative-ttl = 60;
  };
  rebuild.optionalSubstituters = ["http://${functor}:5000?priority=50"];

  nix.distributedBuilds = true;
  nix.buildMachines = [
    {
      hostName = "functor-builder"; # ssh alias below
      protocol = "ssh-ng";
      sshUser = "nixremote";
      sshKey = "/persist/etc/nix/functor-builder_ed25519";
      systems = ["x86_64-linux" "i686-linux"];
      maxJobs = 3;
      speedFactor = 4;
      supportedFeatures = ["nixos-test" "benchmark" "big-parallel" "kvm"];
    }
  ];
  programs.ssh.extraConfig = ''
    Host functor-builder
      HostName ${functor}
      HostKeyAlias functor-builder
      ConnectTimeout 5
      ServerAliveInterval 15
      ServerAliveCountMax 4
      IdentitiesOnly yes
  '';
  programs.ssh.knownHosts.functor-builder.publicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKx7ay3wHtpRS+rRALwtOml5dNrWOmLPwBEBWygvtR1R";
}
