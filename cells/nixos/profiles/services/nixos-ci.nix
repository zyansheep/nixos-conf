# nixos-ci: build this flake's hosts from GitHub so functor's binary cache
# (services.binary-cache) already has them when a machine switches, and record
# which commits build: the gate services.nixos-deploy deploys by.
#
# Runs as the unprivileged nixos-ci user in its own clone under
# /var/lib/nixos-ci (never the ~/nixos-conf working tree), and keeps GC roots
# for the newest 3 builds of each host from main (2 from other branches)
# there. Its builds run in nix-daemon, i.e. in the low-priority
# nix-builds.slice from services.binary-cache.
#
#   nixos-ci run           build origin/main now and follow the log (^C detaches)
#   nixos-ci update        same, with `nix flake update` applied; if every host
#                          builds, `nixos-ci lock` prints that flake.lock
#   nixos-ci status        last result per host, timer
#   nixos-ci results REV…  those commits' results, if built (nixos-deploy asks)
#   nixos-ci log [-f]      journal of all nixos-ci units
#   nixos-ci stop          cancel a running build
#
# - A timer polls GitHub every 5 minutes and builds each branch head it hasn't
#   tried yet, main first; other branches only if their head is under two
#   weeks old. Only this repo's branches: pull requests from forks never run.
# - Each build leaves results/<rev>, in the main.status format ("rev",
#   "built", then "<host> ok <path>" or "<host> FAILED").
# - With a GitHub token in /var/lib/nixos-ci/github-token (owner nixos-ci,
#   mode 0600: a fine-grained token for zyansheep/nixos-conf with only "Commit
#   statuses: Read and write"), each build also sets the commit statuses
#   nixos-ci/<host>: pending, then success (with the store path) or failure.
#   That's what a pull request shows. Without the file it skips them.
#
# Builds log in Nix's internal-json format through `update-manager nixlog`,
# which prints the usual lines to the journal and keeps progress.json (builds
# done/expected, what is building, an ETA) and build-times.json (how long each
# derivation took, for the next ETA) here, for the update-manager widget.
#
# From isomorph: `ssh functor nixos-ci run`.
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

  updateManager = inputs.update-manager.packages.${pkgs.stdenv.hostPlatform.system}.default;
  build = pkgs.writeShellApplication {
    name = "nixos-ci-build";
    runtimeInputs = [config.nix.package pkgs.git pkgs.coreutils pkgs.util-linux pkgs.curl pkgs.jq pkgs.findutils updateManager];
    text = ''
      # nixos-ci-build [--poll] [--update]
      state=${state}
      repo_slug=zyansheep/nixos-conf
      url=https://github.com/$repo_slug.git
      hosts=(${lib.escapeShellArgs hosts})
      mode=main
      for arg in "$@"; do
        case $arg in
          --poll) mode=poll ;;
          --update) mode=update ;;
          *) echo "unknown argument: $arg" >&2; exit 2 ;;
        esac
      done

      # One run at a time: the poll timer, manual runs and update runs.
      exec 9>"$state/lock"
      flock 9
      mkdir -p "$state/results" "$state/tried"

      # Sets the GitHub commit status nixos-ci/<host> on $1: $3 is pending, success
      # or failure, $4 the description (GitHub keeps 140 characters).
      github_status() {
        local token
        token=$(cat "$state/github-token" 2>/dev/null) || return 0
        jq -n --arg state "$3" --arg description "''${4:0:140}" --arg context "nixos-ci/$2" \
          '{$state, $description, $context}' |
          curl --silent --show-error --fail --max-time 20 --output /dev/null \
            -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
            --data @- "https://api.github.com/repos/$repo_slug/statuses/$1" ||
          echo "could not set nixos-ci/$2 on GitHub for ''${1:0:12}"
      }

      # build CHECKOUT REV ROOTS KEEP [STATUS]: build every host at REV in its own
      # checkout, keep KEEP GC roots per host under roots/ROOTS, and write
      # results/REV (and the file STATUS, if given). Returns 1 if a host failed.
      build() {
        local repo=$state/$1 rev=$2 rootdir=$3 keep=$4 status=''${5:-} h d out failed=0
        [ -d "$repo/.git" ] || git clone --quiet "$url" "$repo"
        cd "$repo"
        git fetch --quiet origin "$rev"
        git checkout --quiet --force --detach "$rev"
        git submodule --quiet update --init --recursive --force
        git clean --quiet -ffdx
        echo "building ''${rev:0:12}: $(git log -1 --format=%s | cut -c1-120)"
        if [ "$rootdir" = update ]; then
          nix flake update
        else
          for h in "''${hosts[@]}"; do github_status "$rev" "$h" pending "building on functor"; done
        fi

        local start
        start=$(date +%s)
        declare -A drv=()
        local targets=()
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
          rm -f "''${state:?}/progress.json"
          nix build --no-link --keep-going --max-jobs 2 --cores 8 --log-format internal-json "''${targets[@]}" 2>&1 >/dev/null |
            update-manager nixlog --progress "$state/progress.json" --history "$state/build-times.json" --jobs 2 || true
        fi

        local stamp result roots
        stamp=$(date +%Y%m%d-%H%M%S)
        result=$(mktemp "$state/result.XXXXXX")
        {
          echo "rev     $rev"
          echo "built   $(date -Is) in $((($(date +%s) - start) / 60)) min"
        } >"$result"
        for h in "''${hosts[@]}"; do
          out=
          [ -n "''${drv[$h]:-}" ] && out=$(nix-store --query --outputs "''${drv[$h]}")
          if [ -n "$out" ] && nix-store --check-validity "$out" 2>/dev/null; then
            roots=$state/roots/$rootdir/$h
            mkdir -p "$roots"
            nix-store --realise "$out" --add-root "$roots/$stamp-''${rev:0:12}" >/dev/null
            find "$roots" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | head -n -"$keep" |
              while read -r old; do rm -f "''${roots:?}/''${old:?}"; done
            echo "$h ok $out" | tee -a "$result"
            [ "$rootdir" = update ] || github_status "$rev" "$h" success "$out"
          else
            echo "$h FAILED" | tee -a "$result"
            [ "$rootdir" = update ] || github_status "$rev" "$h" failure "doesn't build (nixos-ci log on functor)"
            failed=1
          fi
        done
        if [ "$rootdir" = update ]; then
          if [ "$failed" = 0 ]; then
            cp flake.lock "$state/update.flake.lock"
            echo "lock    nixos-ci lock" >>"$result"
          fi
        else
          chmod 0644 "$result"
          cp "$result" "$state/results/$rev"
        fi
        [ -z "$status" ] || cp "$result" "$status"
        rm -f "''${result:?}"
        return "$failed"
      }

      main_rev() { git ls-remote "$url" refs/heads/main | cut -f1; }

      case $mode in
        update)
          build checkout-update "$(main_rev)" update 1 "$state/update.status"
          ;;
        main)
          rev=$(main_rev)
          touch "$state/tried/$rev"
          build checkout-main "$rev" main 3 "$state/main.status"
          ;;
        poll)
          # Each head once: recorded before building, so a commit that fails (or
          # kills the build) isn't retried every 5 minutes; `nixos-ci run` always
          # builds main. main goes first.
          failed=0
          heads=$(git ls-remote --heads "$url" | awk '$2 == "refs/heads/main" {print; next} {rest = rest $0 "\n"} END {printf "%s", rest}')
          while read -r rev ref; do
            [ -n "$rev" ] && [ ! -e "$state/tried/$rev" ] || continue
            if [ "$ref" = refs/heads/main ]; then
              touch "$state/tried/$rev"
              build checkout-main "$rev" main 3 "$state/main.status" || failed=1
            else
              repo=$state/checkout-branch
              [ -d "$repo/.git" ] || git clone --quiet "$url" "$repo"
              git -C "$repo" fetch --quiet origin "$rev"
              # Branches nobody has pushed to for two weeks stay unbuilt.
              [ $(($(date +%s) - $(git -C "$repo" log -1 --format=%ct "$rev"))) -lt $((14 * 86400)) ] || continue
              touch "$state/tried/$rev"
              echo "branch ''${ref#refs/heads/}"
              build checkout-branch "$rev" branch 2 || failed=1
            fi
          done <<<"$heads"
          # Commits tried a month ago won't come back.
          find "$state/tried" "$state/results" -type f -mtime +30 -delete
          exit "$failed"
          ;;
      esac
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
        results)
          # One block per built commit, its rev first; commits not built yet are left out.
          shift
          for rev in "$@"; do
            [[ $rev =~ ^[0-9a-f]{40}$ ]] && [ -f "$state/results/$rev" ] || continue
            cat "$state/results/$rev"
            echo
          done
          ;;
        *)
          echo "usage: nixos-ci [run|update|status|results REV...|log [-f]|lock|stop]" >&2
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
      service "--poll"
      // {
        description = "Build the nixos-conf branch heads not tried yet";
        startAt = "*:0/5";
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
