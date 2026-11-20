# input-bump: move chosen flake inputs of nixos-conf to their branch's newest commit, the way z3-autoupdate bump
# moves Z3 Code: in its own clone of origin/main (never ~/nixos-conf), the input's own checks first, then both hosts'
# systems, then a signed commit "flake: <input> <old> -> <new>" pushed to main. nixos-ci and nixos-deploy take it from
# there (functor first). Git only: an input is followed with `git ls-remote` on its URL, whatever hosts it.
#
# Every 30 minutes on functor (input-bump.inputs is set there only). A failure (the input's checks, a host that
# doesn't build) is tried again on the next two runs, then that commit of the input is skipped until a newer one comes.
#
#   input-bump status    each input's locked and newest commit, and the history
#   input-bump run       check now
#   input-bump log [-f]  the unit's journal
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.input-bump;
  user = "zyansheep";

  cli = pkgs.writeShellApplication {
    name = "input-bump";
    runtimeInputs = with pkgs; [config.nix.package coreutils gawk git gnugrep gh jq util-linux];
    text = ''
      state=''${XDG_STATE_HOME:-$HOME/.local/state}/input-bump
      runtime=''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/input-bump
      clone=$state/nixos-conf
      # name url branch checks
      inputs=(${lib.concatMapStringsSep " " (i: lib.escapeShellArg "${i.name} ${i.url} ${i.branch} ${lib.boolToString i.checks}") cfg.inputs})
      mkdir -p "$state/failed" "$runtime"

      say() { echo "input-bump: $*" >&2; }
      record() { printf '%s %s\n' "$(date -Is)" "$*" >>"$state/history"; say "$*"; }

      refresh() {
        [ -d "$clone/.git" ] || git clone --quiet https://github.com/zyansheep/nixos-conf.git "$clone"
        git -C "$clone" fetch --quiet origin main
        git -C "$clone" checkout --quiet --force -B main origin/main
        git -C "$clone" clean --quiet -ffdx
        git -C "$clone" submodule --quiet sync --recursive
        git -C "$clone" submodule --quiet update --init --recursive --force
      }
      locked() { jq -r --arg n "$1" '.nodes[$n].locked.rev // empty' "$clone/flake.lock"; }
      newest() { git ls-remote "$1" "refs/heads/$2" | cut -f1; }

      # $1 input, $2 commit, $3 what failed, $4 log: retried on the next two runs, then skipped.
      failed() {
        local tries
        tries=$(($(cat "$state/failed/$1-$2" 2>/dev/null || echo 0) + 1))
        echo "$tries" >"$state/failed/$1-$2"
        record "$1 ''${2:0:12}: $3 (try $tries of 3; log $4)"
      }

      bump_one() {
        local name=$1 url=$2 branch=$3 checks=$4 old new log
        refresh
        old=$(locked "$name")
        new=$(newest "$url" "$branch")
        [ -n "$new" ] || { say "$name: could not read $branch of $url"; return 1; }
        [ "$old" != "$new" ] || return 0
        [ "$(cat "$state/failed/$name-$new" 2>/dev/null || echo 0)" -lt 3 ] || return 0
        log=$state/$name-''${new:0:12}.log
        : >>"$log"
        (cd "$clone" && nix flake update "$name") >>"$log" 2>&1 || { failed "$name" "$new" "nix flake update failed" "$log"; return 1; }
        # The branch may have moved since ls-remote: what got locked is what is checked and built.
        new=$(locked "$name")
        [ "$old" != "$new" ] || return 0
        [ "$(cat "$state/failed/$name-$new" 2>/dev/null || echo 0)" -lt 3 ] || return 0
        record "$name: ''${old:0:12} -> ''${new:0:12}"
        if [ "$checks" = true ] && ! nice -n 10 nix flake check "git+$url?rev=$new" >>"$log" 2>&1; then
          failed "$name" "$new" "its checks fail" "$log"
          return 1
        fi
        if ! (
          cd "$clone"
          nice -n 10 nix build --no-link --keep-going \
            .#nixosConfigurations.functor.config.system.build.toplevel \
            .#nixosConfigurations.isomorph.config.system.build.toplevel
        ) >>"$log" 2>&1; then
          failed "$name" "$new" "the systems don't build with it" "$log"
          return 1
        fi
        git -C "$clone" commit --quiet -m "flake: $name ''${old:0:12} -> ''${new:0:12}" -- flake.lock
        # main may have moved while the systems built: rebase once and retry.
        if ! git -C "$clone" -c credential.helper= -c credential.helper='!gh auth git-credential' push --quiet origin main:main >>"$log" 2>&1; then
          if ! git -C "$clone" pull --quiet --rebase origin main >>"$log" 2>&1 ||
            ! git -C "$clone" -c credential.helper= -c credential.helper='!gh auth git-credential' push --quiet origin main:main >>"$log" 2>&1; then
            record "$name: push failed; the next run tries again"
            return 1
          fi
        fi
        record "$name: pushed ''${new:0:12}"
        systemctl start --no-block nixos-ci.service 2>/dev/null || true
      }

      run() {
        local line rc=0
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        for line in "''${inputs[@]}"; do
          # shellcheck disable=SC2086
          bump_one $line || rc=1
        done
        return "$rc"
      }

      status() {
        local line name url branch _
        [ -d "$clone/.git" ] && git -C "$clone" fetch --quiet origin main && git -C "$clone" checkout --quiet --force -B main origin/main
        for line in "''${inputs[@]}"; do
          read -r name url branch _ <<<"$line"
          printf '%-16s locked %.12s  %s %.12s\n' "$name" "$(locked "$name" 2>/dev/null)" "$branch" "$(newest "$url" "$branch")"
        done
        echo
        tail -n 10 "$state/history" 2>/dev/null || true
      }

      case ''${1:-status} in
        status) status ;;
        run) run ;;
        log) shift; journalctl --user --no-pager --lines 200 -u input-bump.service "$@" ;;
        *) echo "usage: input-bump [status|run|log [-f]]" >&2; exit 2 ;;
      esac
    '';
  };
in {
  options.input-bump.inputs = lib.mkOption {
    type = lib.types.listOf (lib.types.submodule {
      options = {
        name = lib.mkOption {
          type = lib.types.str;
          description = "The input's name in flake.nix.";
        };
        url = lib.mkOption {
          type = lib.types.str;
          description = "Its git URL (https), for `git ls-remote` and its checks.";
        };
        branch = lib.mkOption {
          type = lib.types.str;
          default = "main";
        };
        checks = lib.mkOption {
          type = lib.types.bool;
          default = true;
          description = "Run the input's own `nix flake check` at the new commit before bumping.";
        };
      };
    });
    default = [];
    description = "Flake inputs this host moves to their branch's newest commit (one host only: functor).";
  };

  config = lib.mkIf (cfg.inputs != []) {
    environment.systemPackages = [cli];
    systemd.user.services.input-bump = {
      description = "Move nixos-conf's followed flake inputs to their newest commits";
      unitConfig.ConditionUser = user;
      restartIfChanged = false;
      environment.PATH = lib.mkForce "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin:%h/.nix-profile/bin";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${lib.getExe cli} run";
        Nice = 10;
      };
    };
    systemd.user.timers.input-bump = {
      unitConfig.ConditionUser = user;
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "*:10/30";
        Persistent = true;
      };
    };
  };
}
