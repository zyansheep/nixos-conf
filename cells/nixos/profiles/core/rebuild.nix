# Passwordless `rebuild` for zyansheep.
#
# `rebuild` runs `nixos-rebuild switch --flake <repo>#<host>` as root; the
# sudo-rs rule below lets zyansheep invoke ONLY that immutable store-path
# wrapper with no password (same scoped-rule pattern as hampshire-vpn). This
# enables unattended rebuilds (e.g. driven by an agent) without loosening sudo
# in general.
#
# SECURITY: because the wrapper activates whatever the flake evaluates to, and
# the flake dir is user-writable, this is effectively passwordless root for
# zyansheep. That's an accepted trade-off on a single-user workstation; the
# alternative broad switch is `security.sudo-rs.wheelNeedsPassword = false`.
#
# Flakes ignore untracked files, so `git add` new files before rebuilding
# (the `nrb` alias does `git add .` first).
_: {
  pkgs,
  config,
  ...
}: let
  flakeDir = "/home/zyansheep/nixos-conf";
  rebuild = pkgs.writeShellScriptBin "rebuild" ''
    set -euo pipefail
    # Pre-authorize the user-owned repo for root's git so a dirty-tree eval
    # can't trip "detected dubious ownership" (harmless otherwise).
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0=safe.directory
    export GIT_CONFIG_VALUE_0=${flakeDir}
    # Trust the flake's own nixConfig (extra binary caches, experimental
    # features) under root too, so rebuilds use nrdxp/nix-community caches
    # instead of building from source.
    export NIX_CONFIG="accept-flake-config = true"
    exec ${config.system.build.nixos-rebuild}/bin/nixos-rebuild switch \
      --flake ${flakeDir}#${config.networking.hostName} "$@"
  '';
in {
  environment.systemPackages = [rebuild];

  # Scoped to ONLY the wrapper above — every other sudo still needs a password.
  security.sudo-rs.extraRules = [
    {
      users = ["zyansheep"];
      commands = [
        {
          command = "/run/current-system/sw/bin/rebuild";
          options = ["NOPASSWD"];
        }
      ];
    }
  ];
}
