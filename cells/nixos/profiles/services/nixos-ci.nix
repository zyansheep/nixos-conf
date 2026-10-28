# nixos-ci: build this flake's hosts from GitHub (origin/main) so functor's
# binary cache (services.binary-cache) already has them when a laptop rebuilds.
#
# Runs as the unprivileged nixos-ci user in its own clone under
# /var/lib/nixos-ci (never the ~/nixos-conf working tree), and keeps GC roots
# for the newest 3 builds of each host there. Its builds run in nix-daemon,
# i.e. in the low-priority nix-builds.slice from services.binary-cache.
#
#   nixos-ci run       build origin/main now and follow the log (^C detaches)
#   nixos-ci update    same, with `nix flake update` applied; if every host
#                      builds, `nixos-ci lock` prints that flake.lock
#   nixos-ci status    last result per host, timer
#   nixos-ci log [-f]  journal of all nixos-ci units
#   nixos-ci stop      cancel a running build
#
# From isomorph: `ssh functor nixos-ci run`. A timer polls origin/main every
# 15 minutes and builds new commits.
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  state = "/var/lib/nixos-ci";
  hosts = ["isomorph" "functor"];
  units = ["nixos-ci.service" "nixos-ci-poll.service" "nixos-ci-update.service"];

  build = pkgs.writeShellApplication {
    name = "nixos-ci-build";
    runtimeInputs = [config.nix.package pkgs.git pkgs.coreutils pkgs.util-linux];
    text = ''
      # nixos-ci-build [--if-changed] [--update]
      state=${state}
      url=https://github.com/zyansheep/nixos-conf.git
      hosts=(${lib.escapeShellArgs hosts})
      mode=main
      keep=3
      if_changed=
      for arg in "$@"; do
        case $arg in
          --if-changed) if_changed=1 ;;
          --update) mode=update keep=1 ;;
          *) echo "unknown argument: $arg" >&2; exit 2 ;;
        esac
      done

      # One run at a time: the poll timer, manual runs and update runs.
      exec 9>"$state/lock"
      flock 9

      repo=$state/checkout-$mode
      [ -d "$repo/.git" ] || git clone --quiet "$url" "$repo"
      cd "$repo"
      git fetch --quiet origin main
      rev=$(git rev-parse FETCH_HEAD)
      if [ -n "$if_changed" ] && [ "$(cat "$state/$mode.rev" 2>/dev/null)" = "$rev" ]; then
        echo "origin/main is still ''${rev:0:12}, already built"
        exit 0
      fi
      # Recorded before building, so the timer doesn't retry a failing commit
      # every 15 minutes; `nixos-ci run` always builds.
      echo "$rev" >"$state/$mode.rev"
      git checkout --quiet --force --detach "$rev"
      git submodule --quiet update --init --recursive --force
      git clean --quiet -ffdx
      echo "building ''${rev:0:12}: $(git log -1 --format=%s)"
      if [ "$mode" = update ]; then
        nix flake update
      fi

      start=$(date +%s)
      declare -A drv=()
      targets=()
      for h in "''${hosts[@]}"; do
        if d=$(nix eval --raw ".#nixosConfigurations.$h.config.system.build.toplevel.drvPath"); then
          drv[$h]=$d
          targets+=("$d^*")
        else
          echo "$h: evaluation failed"
        fi
      done
      # Failed builds show up below as missing outputs; --keep-going builds
      # everything that doesn't depend on a failure.
      if [ ''${#targets[@]} -gt 0 ]; then
        nix build --no-link --keep-going --max-jobs 2 --cores 8 "''${targets[@]}" || true
      fi

      stamp=$(date +%Y%m%d-%H%M%S)
      status=$state/$mode.status
      {
        echo "rev     $rev"
        echo "built   $(date -Is) in $((($(date +%s) - start) / 60)) min"
      } >"$status.tmp"
      failed=0
      for h in "''${hosts[@]}"; do
        out=
        [ -n "''${drv[$h]:-}" ] && out=$(nix-store --query --outputs "''${drv[$h]}")
        if [ -n "$out" ] && nix-store --check-validity "$out" 2>/dev/null; then
          roots=$state/roots/$mode/$h
          mkdir -p "$roots"
          nix-store --realise "$out" --add-root "$roots/$stamp-''${rev:0:12}" >/dev/null
          find "$roots" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | head -n -"$keep" |
            while read -r old; do rm -f "$roots/$old"; done
          echo "$h ok $out" | tee -a "$status.tmp"
        else
          echo "$h FAILED" | tee -a "$status.tmp"
          failed=1
        fi
      done
      if [ "$mode" = update ] && [ "$failed" = 0 ]; then
        cp flake.lock "$state/update.flake.lock"
        echo "lock    nixos-ci lock" >>"$status.tmp"
      fi
      mv "$status.tmp" "$status"
      exit "$failed"
    '';
  };

  cli = pkgs.writeShellApplication {
    name = "nixos-ci";
    runtimeInputs = [pkgs.systemd pkgs.coreutils];
    text = ''
      state=${state}
      units=(${lib.escapeShellArgs units})
      case ''${1:-status} in
        run | update)
          unit=nixos-ci.service
          [ "$1" = update ] && unit=nixos-ci-update.service
          journalctl --unit "$unit" --follow --lines 0 --output cat &
          follow=$!
          trap 'kill "$follow"' EXIT
          # Blocks until the oneshot finishes. ^C only detaches; the build
          # goes on (nixos-ci stop cancels it).
          systemctl start "$unit"
          ;;
        stop) systemctl stop "''${units[@]}" ;;
        status)
          for mode in main update; do
            [ -f "$state/$mode.status" ] && { echo "== $mode"; cat "$state/$mode.status"; echo; }
          done
          systemctl list-units --all --no-pager --no-legend "''${units[@]}"
          systemctl list-timers --no-pager nixos-ci-poll.timer
          ;;
        log)
          shift
          journalctl --no-pager --lines 200 "''${units[@]/#/--unit=}" "$@"
          ;;
        lock) cat "$state/update.flake.lock" ;;
        *)
          echo "usage: nixos-ci [run|update|status|log [-f]|lock|stop]" >&2
          exit 2
          ;;
      esac
    '';
  };

  service = args: {
    after = ["network-online.target"];
    wants = ["network-online.target"];
    # A switch mid-build shouldn't kill it.
    restartIfChanged = false;
    environment = {
      HOME = state;
      SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt"; # git and nix fetch over https
    };
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${lib.getExe build} ${args}";
      User = "nixos-ci";
      Group = "nixos-ci";
      StateDirectory = "nixos-ci";
      StateDirectoryMode = "0755"; # status files readable by `nixos-ci status`
      SyslogIdentifier = "nixos-ci";
      # Evaluation happens in this unit; it shares the builds' low weight.
      Slice = "nix-builds.slice";
      Nice = 10;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
    };
  };
in {
  users.users.nixos-ci = {
    isSystemUser = true;
    group = "nixos-ci";
    home = state;
  };
  users.groups.nixos-ci = {};

  systemd.services = {
    nixos-ci = service "" // {description = "Build nixos-conf hosts from origin/main";};
    nixos-ci-update = service "--update" // {description = "Build nixos-conf hosts with updated flake inputs";};
    nixos-ci-poll =
      service "--if-changed"
      // {
        description = "Build nixos-conf hosts if origin/main moved";
        startAt = "*:0/15";
      };
  };

  environment.systemPackages = [cli];

  # Let wheel run, cancel and restart the builds (e.g. over ssh) without sudo.
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          subject.isInGroup("wheel")) {
        var unit = action.lookup("unit");
        var units = ${builtins.toJSON units};
        if (units.indexOf(unit) >= 0 &&
            ["start", "stop", "restart"].indexOf(action.lookup("verb")) >= 0) {
          return polkit.Result.YES;
        }
      }
    });
  '';
}
