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
#
# `rebuild --build-only` builds without switching (`nixos-rebuild build`, so
# ./result), with the same cache and local-compile checks; it needs no root.
# update-manager's ask-rebuild (`nrb-request`, services/update-manager.nix)
# uses it to show the user what an agent's requested switch would change.
#
# A switch is refused when the tree doesn't contain the commit this machine
# runs (`nixos-version --configuration-revision`): a stale ~/nixos-conf would
# otherwise undo what nixos-deploy (services.nixos-deploy) deployed from main.
# `rebuild --allow-downgrade` switches anyway.
_: {
  pkgs,
  config,
  lib,
  ...
}: let
  cfg = config.rebuild;
  flakeDir = "/home/zyansheep/nixos-conf";
  toplevel = "${flakeDir}#nixosConfigurations.${config.networking.hostName}.config.system.build.toplevel";
  # Prints the derivations that building the flake output $1 would compile on
  # this machine: not built yet and not substitutable from a cache that answers,
  # not preferLocalBuild, and built with a stdenv that has a C compiler.
  # Trivial builders (writeText, runCommand, buildEnv, unit files) are
  # preferLocalBuild or use stdenvNoCC, so they don't count.
  localCompiles = pkgs.writeShellScript "rebuild-local-compiles" ''
    set -euo pipefail
    PATH=${lib.makeBinPath [config.nix.package pkgs.jq pkgs.gawk pkgs.gnused pkgs.coreutils]}
    if ! out=$(nix build --dry-run "$1" 2>&1); then
      echo "$out" >&2
      exit 1
    fi
    drvs=$(awk '/will be built:$/ { b = 1; next } b && /^ / { print $1; next } { b = 0 }' <<<"$out")
    [ -n "$drvs" ] || exit 0
    # `nix derivation show` keys and inputs are store basenames in newer Nix
    # and full paths in older Nix; the jq below accepts both.
    show=$(nix derivation show $drvs)
    inputs='(.inputs.drvs // .inputDrvs // {}) | keys | map(sub(".*/"; ""))'
    stdenvs=$(jq -r "(.derivations // .)[] | $inputs | .[] | select(endswith(\"-stdenv-linux.drv\"))" <<<"$show" | sort -u)
    [ -n "$stdenvs" ] || exit 0
    cc=$(nix derivation show $(sed 's|^|/nix/store/|' <<<"$stdenvs") |
      jq -c "[(.derivations // .) | to_entries[] | select(.value | $inputs | any(test(\"-(gcc|clang)-wrapper-\"))) | .key | sub(\".*/\"; \"\")]")
    jq -r --argjson cc "$cc" "(.derivations // .) | to_entries[]
      | select(.value.env.preferLocalBuild != \"1\")
      | select((.value.structuredAttrs // (.value.env.__json // \"{}\" | fromjson)).preferLocalBuild != true)
      | select(.value | $inputs | any(.[]; IN(\$cc[])))
      | .key | sub(\".*/[a-z0-9]{32}-|^[a-z0-9]{32}-\"; \"\") | sub(\"[.]drv$\"; \"\")" <<<"$show" | sort -u
  '';
  rebuild = pkgs.writeShellScriptBin "rebuild" ''
    set -euo pipefail
    allow_local=
    allow_downgrade=
    action=switch
    args=()
    for arg in "$@"; do
      case $arg in
        --allow-local-builds) allow_local=1 ;;
        --allow-downgrade) allow_downgrade=1 ;;
        --build-only) action=build ;;
        *) args+=("$arg") ;;
      esac
    done
    # Pre-authorize the user-owned repo for root's git so a dirty-tree eval
    # can't trip "detected dubious ownership" (harmless otherwise).
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0=safe.directory
    export GIT_CONFIG_VALUE_0=${flakeDir}
    if [ "$action" = switch ] && [ -z "$allow_downgrade" ]; then
      git=${pkgs.git}/bin/git
      running=$(/run/current-system/sw/bin/nixos-version --configuration-revision 2>/dev/null || true)
      running=''${running%-dirty}
      head=$($git -C ${flakeDir} rev-parse HEAD)
      if [[ $running =~ ^[0-9a-f]{40}$ ]] && [ "$running" != "$head" ] &&
        ! $git -C ${flakeDir} merge-base --is-ancestor "$running" HEAD 2>/dev/null; then
        if $git -C ${flakeDir} cat-file -e "$running^{commit}" 2>/dev/null; then
          echo "rebuild: ${flakeDir} (at ''${head:0:12}) doesn't contain ''${running:0:12}, the commit this machine runs: switching would undo it" >&2
        else
          echo "rebuild: this machine runs commit ''${running:0:12}, which ${flakeDir} doesn't have: fetch it first" >&2
        fi
        echo "rebuild: pull or rebase onto it, or pass --allow-downgrade" >&2
        exit 1
      fi
    fi
    # Trust the flake's own nixConfig (extra binary caches, experimental
    # features) under root too, so rebuilds use nrdxp/nix-community caches
    # instead of building from source.
    export NIX_CONFIG="accept-flake-config = true"
    # Caches that are only worth asking when they answer right away (another
    # machine over the tailnet): add the ones that reply within 2 s.
    unreachable=()
    for url in ${lib.escapeShellArgs cfg.optionalSubstituters}; do
      if ${pkgs.curl}/bin/curl --silent --fail --max-time 2 --output /dev/null "''${url%%\?*}/nix-cache-info"; then
        NIX_CONFIG+=$'\nextra-substituters = '"$url"
      else
        echo "rebuild: $url did not answer, not using it" >&2
        unreachable+=("''${url%%\?*}")
      fi
    done
    ${lib.optionalString cfg.refuseLocalCompiles ''
      if [ ''${#unreachable[@]} -gt 0 ] && [ -z "$allow_local" ]; then
        echo "rebuild: checking what would compile here..." >&2
        compiles=$(${localCompiles} ${toplevel})
        if [ -n "$compiles" ]; then
          echo "rebuild: these would compile on this machine:" >&2
          echo "$compiles" | ${pkgs.gnused}/bin/sed 's/^/  /' >&2
          echo "rebuild: retry when ''${unreachable[*]} answers, or pass --allow-local-builds" >&2
          exit 1
        fi
      fi
    ''}
    exec ${config.system.build.nixos-rebuild}/bin/nixos-rebuild "$action" \
      --flake ${flakeDir}#${config.networking.hostName} "''${args[@]}"
  '';
in {
  options.rebuild.optionalSubstituters = lib.mkOption {
    type = lib.types.listOf lib.types.str;
    default = [];
    description = ''
      Substituters `rebuild` adds only if they answer within 2 seconds, for
      caches that are often unreachable. In nix.conf, an unreachable cache
      delays every download while Nix retries it.
    '';
  };

  options.rebuild.refuseLocalCompiles = lib.mkOption {
    type = lib.types.bool;
    default = false;
    description = ''
      When an optional substituter doesn't answer, refuse to switch if the
      system would compile a package here: a derivation built with a C
      compiler that isn't preferLocalBuild. For a laptop whose build machine
      is sometimes out of reach.
      `rebuild --allow-local-builds` (or `nrb --allow-local-builds`) skips the
      check.
    '';
  };

  config.environment.systemPackages = [rebuild];

  # Scoped to ONLY the wrapper above — every other sudo still needs a password.
  config.security.sudo-rs.extraRules = [
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
