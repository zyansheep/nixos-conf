# z3-autoupdate: keep Z3 Code (the zyansheep/t3code fork, cells/common/packages/z3code.nix and t3-cli.nix) on
# the newest release, which follows upstream's nightly.
#
# - bump (functor, hourly; z3-autoupdate.bump): in its own clone of origin/main (never ~/nixos-conf), points the
#   two packages at the newest release, builds both hosts' systems, and commits "z3code: <version>" and pushes it
#   to main. nixos-ci then caches the systems for isomorph.
# - apply (every host, every 15 minutes): when origin/main pins a version this host doesn't run, waits until it is
#   idle (no T3 agent run in progress, nobody at the machine for 15 minutes or the screen locked, on AC power, no
#   builds), fast-forwards ~/nixos-conf to origin/main (uncommitted changes stay, as with `nrb`), runs
#   `sudo rebuild`, restarts what runs Z3 Code (z3-autoupdate.restartUnits, the desktop app) and checks it.
# - On a failed check it switches back to the previous system (update-manager-switch@, through polkit), restarts
#   the old version, pauses itself and starts a T3 thread in the nixos-conf project on this host to find out why.
#   That thread fixes what is plainly fixable and runs `z3-autoupdate resume`; otherwise it leaves the update
#   paused for review. A failed build in bump pauses the same way (nothing is pushed).
#
#   z3-autoupdate status           versions, gates, pause, last result
#   z3-autoupdate apply [--force]  apply now (--force skips the idle gates)
#   z3-autoupdate bump             (functor) check for a new release now
#   z3-autoupdate pause [reason] / resume
#   z3-autoupdate log [-f]         the units' journal
#
# Like core.rebuild, apply needs passwordless `sudo rebuild`, and the rollback uses update-manager's switch units.
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.z3-autoupdate;
  user = "zyansheep";

  cli = pkgs.writeShellApplication {
    name = "z3-autoupdate";
    runtimeInputs = with pkgs; [
      config.nix.package
      coreutils
      curl
      gawk
      gh
      git
      gnugrep
      gnused
      jq
      libnotify
      procps
      sqlite
      systemd
      util-linux
    ];
    text = ''
      repo=https://github.com/zyansheep/t3code
      conf=$HOME/nixos-conf
      state=''${XDG_STATE_HOME:-$HOME/.local/state}/z3-autoupdate
      runtime=''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/z3-autoupdate
      host=${config.networking.hostName}
      bump_host=${lib.boolToString cfg.bump}
      restart_units=(${lib.escapeShellArgs cfg.restartUnits})
      desktop_app=${lib.boolToString cfg.desktopApp}
      t3_db=$HOME/.t3/userdata/statev2.sqlite
      runtime_json=$HOME/.t3/userdata/server-runtime.json
      packages=(cells/common/packages/z3code.nix cells/common/packages/t3-cli.nix)
      mkdir -p "$state" "$runtime"

      say() { echo "z3-autoupdate: $*" >&2; }
      record() { printf '%s %s\n' "$(date -Is)" "$*" >>"$state/history"; say "$*"; }
      notify() {
        # Only reaches a graphical session on this host; the T3 thread is the durable signal.
        notify-send --app-name "Z3 Code update" "$1" "''${2:-}" 2>/dev/null || true
      }

      # The version a nixos-conf tree pins, from t3-cli.nix (z3code.nix pins the same).
      pinned() { sed -n 's/^  version = "\(.*\)";$/\1/p' "$1"; }
      # The version this host's profile runs, from the t3 CLI's store path.
      running() {
        local t3
        t3=$(readlink -f "/etc/profiles/per-user/$USER/bin/t3" 2>/dev/null || true)
        sed -n 's|^/nix/store/[^/]*-t3-cli-\(.*\)/bin/t3$|\1|p' <<<"$t3"
      }
      origin_version() {
        git -C "$conf" fetch --quiet origin main
        git -C "$conf" show origin/main:cells/common/packages/t3-cli.nix | sed -n 's/^  version = "\(.*\)";$/\1/p'
      }

      paused() { [ -e "$state/paused" ]; }
      pause() {
        { echo "since   $(date -Is)"; echo "reason  $1"; } >"$state/paused"
        record "paused: $1"
      }

      # --- idle gates -------------------------------------------------------------------------------------------
      # Whether this user has an active Wayland session here (functor usually sits at the greeter).
      graphical_session() {
        local s
        while read -r s _; do
          [ "$(loginctl show-session "$s" -p Name --value 2>/dev/null)" = "$USER" ] || continue
          [ "$(loginctl show-session "$s" -p Type --value 2>/dev/null)" = wayland ] || continue
          [ "$(loginctl show-session "$s" -p State --value 2>/dev/null)" = active ] && return 0
        done < <(loginctl list-sessions --no-legend 2>/dev/null)
        return 1
      }

      # Prints why this host is busy, or nothing.
      busy_reason() {
        if [ -r "$t3_db" ]; then
          local runs
          # Runs left "running" by a crash would block forever, so only recent ones count.
          runs=$(sqlite3 -readonly "$t3_db" "select count(*) from orchestration_v2_projection_runs
            where status in ('queued', 'preparing', 'starting', 'running')
              and julianday(requested_at) > julianday('now', '-12 hours')" 2>/dev/null || echo 0)
          [ "''${runs:-0}" -gt 0 ] && { echo "$runs T3 agent run(s) in progress"; return; }
        fi
        if graphical_session && [ ! -e "$runtime/idle" ] && ! pgrep -x swaylock >/dev/null; then
          echo "someone is at the machine (input in the last 15 minutes, screen unlocked)"
          return
        fi
        local supply on_ac=0 battery=0
        for supply in /sys/class/power_supply/*; do
          [ -r "$supply/type" ] || continue
          case $(cat "$supply/type") in
            Battery) battery=1 ;;
            Mains | USB) [ "$(cat "$supply/online" 2>/dev/null)" = 1 ] && on_ac=1 ;;
          esac
        done
        [ "$battery" = 1 ] && [ "$on_ac" = 0 ] && { echo "on battery"; return; }
        if systemctl is-active --quiet nixos-ci.service nixos-ci-update.service nixos-ci-poll.service 2>/dev/null; then
          echo "nixos-ci is building"
          return
        fi
        # Nix builds run as nixbld users; cargo and rustc are local Rust builds.
        if pgrep -G nixbld >/dev/null || pgrep -x 'cargo|rustc|nixos-rebuild' >/dev/null; then
          echo "a build is running"
          return
        fi
        return 0
      }

      # --- the T3 debug thread ----------------------------------------------------------------------------------
      start_thread() {
        local title=$1 body=$2 out link
        t3 project add "$conf" >/dev/null 2>&1 || true
        if out=$(printf '%s\n' "$body" | t3 thread start "$conf" - --title "$title" --json 2>>"$state/thread.err"); then
          link=$(jq -r '.link // .threadId // empty' <<<"$out" 2>/dev/null || true)
          echo "thread  ''${link:-$out}" >>"$state/paused"
          record "debug thread: ''${link:-$out}"
          notify "$title" "A T3 thread is looking into it: ''${link:-see z3-autoupdate status}"
        else
          record "could not start the debug thread (see $state/thread.err)"
          notify "$title" "Paused. Could not start a T3 thread; see z3-autoupdate status."
        fi
      }

      debug_prompt() {
        # $1 what failed, $2 log file, $3 extra context
        cat <<EOF
      Z3 Code's automatic update failed on $host. Find out why.

      What happened: $1
      $3

      The update is paused (\`z3-autoupdate status\` on $host; its state is in $state, the log of this attempt is
      $2, and \`z3-autoupdate log\` shows the units' journal). How it works is in
      ~/nixos-conf/cells/nixos/profiles/services/z3-autoupdate.nix: bump (functor) pins a new release of
      zyansheep/t3code in cells/common/packages/z3code.nix and t3-cli.nix and pushes to main; apply (each host)
      fast-forwards ~/nixos-conf, runs \`sudo rebuild\`, restarts Z3 Code and checks it.

      Last lines of the log:
      $(tail -n 60 "$2" 2>/dev/null)

      If the cause is clear and the fix is small and safe (a hash or version, a missing library or package, a unit
      or wrapper setting, a flaky check in z3-autoupdate itself), fix it: work in a fresh worktree of nixos-conf off
      origin/main (never in ~/nixos-conf, which may hold uncommitted work), build both hosts
      (\`nix build --no-link .#nixosConfigurations.{functor,isomorph}.config.system.build.toplevel\`), commit and
      push to main, then run \`z3-autoupdate resume\` on $host: it applies once the host is idle, after this thread's
      turn ends. Don't run \`z3-autoupdate apply --force\` yourself: it restarts the T3 server (or desktop app) this
      thread runs on. Don't touch ~/.t3 data, and don't restart a T3 server while one of its threads is running.

      If it isn't clear, or the fix means a change to Z3 Code itself (the zyansheep/t3code fork), a downgrade, or
      anything risky, change nothing: leave the update paused, and end with what broke, the evidence, and the
      options, for the user to review.
      EOF
      }

      # --- checks after a switch ----------------------------------------------------------------------------------
      failed_units() {
        { systemctl --failed --no-legend --plain 2>/dev/null; systemctl --user --failed --no-legend --plain 2>/dev/null; } |
          awk '{print $1}' | sort -u
      }

      desktop_pid() {
        # The Electron main process: the extracted t3code binary with no --type flag.
        pgrep -u "$USER" -f '/nix/store/[^/]*-t3code-[^/]*-extracted/t3code$' | head -n 1
      }

      restart_z3() {
        local unit pid
        for unit in "''${restart_units[@]}"; do
          systemctl --user restart "$unit" || true
        done
        if [ "$desktop_app" = true ] && [ -e "$runtime/desktop-was-running" ]; then
          pid=$(desktop_pid || true)
          if [ -n "$pid" ]; then
            kill -TERM "$pid" 2>/dev/null || true
            for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
            kill -KILL "$pid" 2>/dev/null || true
          fi
          systemd-run --user --collect --quiet --unit "z3code-relaunch-$(date +%s)" \
            --setenv=PATH="/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:/run/wrappers/bin" \
            -- t3code || true
        fi
      }

      # The version this host's T3 server (or desktop app's backend) runs, if one is up.
      server_version() {
        local pid
        pid=$(jq -r '.pid // empty' "$runtime_json" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
          jq -r '.serverVersion // empty' "$runtime_json" 2>/dev/null || true
        fi
      }

      # Waits for this host's T3 server to report version $1 in server-runtime.json.
      wait_server() {
        local want=$1 have=
        for _ in $(seq 90); do
          have=$(jq -r '.serverVersion // empty' "$runtime_json" 2>/dev/null || true)
          if [ "$have" = "$want" ] && kill -0 "$(jq -r '.pid' "$runtime_json")" 2>/dev/null; then
            return 0
          fi
          sleep 2
        done
        echo "the T3 server reports '$have', not $want, after 3 minutes"
        return 1
      }

      # Prints what is wrong after switching to version $1, or nothing. $2: units failed before the switch.
      health() {
        local want=$1 before=$2 have unit new
        have=$(running)
        [ "$have" = "$want" ] || { echo "the profile's t3 is '$have', not $want"; return; }
        "/etc/profiles/per-user/$USER/bin/t3" --version >/dev/null 2>&1 || { echo "t3 --version fails"; return; }
        for unit in "''${restart_units[@]}"; do
          systemctl --user is-active --quiet "$unit" || { echo "$unit is not active after the restart"; return; }
        done
        if [ "''${#restart_units[@]}" -gt 0 ] || [ -e "$runtime/desktop-was-running" ]; then
          wait_server "$want" || return
          t3 thread list --project "$conf" --json >/dev/null 2>&1 || t3 project add "$conf" >/dev/null 2>&1 ||
            { echo "the T3 server does not answer the CLI"; return; }
        fi
        new=$(comm -13 <(echo "$before") <(failed_units))
        if [ -n "$new" ]; then echo "units failed after the switch: $(echo "$new" | tr '\n' ' ')"; fi
        return 0
      }

      # --- apply --------------------------------------------------------------------------------------------------
      apply() {
        local force=$1 want have reason prev log before problem
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        if paused; then say "paused ($(sed -n 's/^reason  //p' "$state/paused"))"; return 0; fi

        want=$(origin_version)
        have=$(running)
        [ -n "$want" ] || { say "could not read the version on origin/main"; return 1; }
        local only_restart=0 server
        if [ "$want" = "$have" ]; then
          # The profile is current; what runs may not be (a switch restarts neither the server nor the app).
          server=$(server_version)
          if [ -z "$server" ] || [ "$server" = "$want" ]; then say "up to date ($have)"; return 0; fi
          only_restart=1
          have=$server
        fi
        # Never downgrade: a main that pins an older release (a hand rebuild ran ahead of it) waits for a newer one.
        if [ -n "$have" ] && [ "$(printf '%s\n' "$want" "$have" | sort -V | tail -n 1)" = "$have" ]; then
          say "main pins $want, older than the running $have; nothing to do"
          return 0
        fi
        if [ "$force" != 1 ]; then
          reason=$(busy_reason || true)
          if [ -n "$reason" ]; then
            echo "$(date -Is) waiting for $want: $reason" >"$state/waiting"
            say "waiting to apply $want: $reason"
            return 0
          fi
        fi
        rm -f "$state/waiting"

        log=$state/apply-$want.log
        : >"$log"
        before=$(failed_units)
        rm -f "$runtime/desktop-was-running"
        if [ "$desktop_app" = true ] && [ -n "$(desktop_pid || true)" ]; then touch "$runtime/desktop-was-running"; fi
        if [ "$only_restart" = 1 ]; then
          record "restarting Z3 Code on $want (it runs $have)"
          restart_z3
          problem=$(health "$want" "$before" 2>&1 | tee -a "$log" || true)
          if [ -z "$problem" ]; then record "restarted on $want"; return 0; fi
          record "restart on $want failed: $problem"
          pause "restarting on $want failed: $problem"
          start_thread "Z3 Code $want failed to start on $host" "$(debug_prompt "restarting Z3 Code on $want (it ran $have): $problem" "$log" "No system was switched; the profile already had $want.")"
          return 1
        fi
        record "applying $want (running $have)"
        if [ "$(git -C "$conf" symbolic-ref --short HEAD 2>/dev/null)" != main ] ||
          ! git -C "$conf" merge --ff-only --quiet origin/main >>"$log" 2>&1; then
          # Local commits or edits in the way: wait (not pause), so it goes through once the tree is pushed or synced.
          reason="$conf can't fast-forward to origin/main (not on main, local commits, or local edits to the same files)"
          echo "$(date -Is) waiting for $want: $reason" >"$state/waiting"
          if [ ! -e "$state/told-$want" ]; then
            touch "$state/told-$want"
            record "waiting for $want: $reason"
            notify "Z3 Code $want waiting" "$reason. It applies once the tree can fast-forward."
          fi
          return 0
        fi
        # The flake reads submodules (?submodules=1): check out the commits main records.
        git -C "$conf" submodule --quiet update --init --recursive >>"$log" 2>&1 || true

        prev=$(readlink -f /run/current-system)

        # The log is ours to write; only rebuild runs as root.
        # shellcheck disable=SC2024
        if ! /run/wrappers/bin/sudo -n /run/current-system/sw/bin/rebuild >>"$log" 2>&1; then
          if grep -q 'retry when .* answers, or pass --allow-local-builds' "$log"; then
            # The laptop would compile locally because functor's cache is out of reach: try again later.
            record "rebuild deferred: functor's cache did not answer"
            return 0
          fi
          problem="sudo rebuild failed"
        else
          restart_z3
          problem=$(health "$want" "$before" 2>&1 | tee -a "$log" || true)
        fi
        if [ -z "$problem" ]; then
          record "applied $want"
          notify "Z3 Code updated" "$want"
          return 0
        fi

        record "apply of $want failed: $problem"
        local rolled="Nothing was switched."
        if [ "$(readlink -f /run/current-system)" != "$prev" ]; then
          # The instance is the escaped store name: a bare "-" would decode to "/".
          local unit
          unit="update-manager-switch@$(systemd-escape "$(basename "$prev")").service"
          if systemctl start "$unit" >>"$log" 2>&1; then
            restart_z3
            rolled="It was rolled back to the previous system ($prev), and Z3 Code restarted on it."
          else
            rolled="Rolling back to $prev FAILED ($unit); the new system is still active."
          fi
        fi
        record "$rolled"
        pause "applying $want failed: $problem"
        start_thread "Z3 Code $want failed on $host" "$(debug_prompt "applying $want (from $have): $problem" "$log" "$rolled")"
        return 1
      }

      # --- bump (functor) -----------------------------------------------------------------------------------------
      push() {
        git -C "$1" -c credential.helper= -c credential.helper='!gh auth git-credential' push --quiet origin main:main
      }

      bump() {
        local tag version clone log sums appimage cli current
        [ "$bump_host" = true ] || { say "this host doesn't bump"; return 0; }
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        if paused; then say "paused"; return 0; fi

        tag=$(gh api repos/zyansheep/t3code/releases/latest -q .tag_name)
        [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+-.*-z3\.[0-9]+$ ]] || { say "latest release '$tag' is not a z3 release"; return 0; }
        version=''${tag#v}

        clone=$state/nixos-conf
        [ -d "$clone/.git" ] || git clone --quiet https://github.com/zyansheep/nixos-conf.git "$clone"
        git -C "$clone" fetch --quiet origin main
        git -C "$clone" checkout --quiet --force -B main origin/main
        git -C "$clone" clean --quiet -ffdx
        git -C "$clone" submodule --quiet sync --recursive
        git -C "$clone" submodule --quiet update --init --recursive --force
        current=$(pinned "$clone/cells/common/packages/t3-cli.nix")
        if [ "$current" = "$version" ]; then say "main already pins $version"; return 0; fi

        log=$state/bump-$version.log
        : >"$log"
        record "bumping $current -> $version"
        sums=$(curl -fsSL "$repo/releases/download/$tag/SHA256SUMS")
        appimage=$(awk -v f="Z3-Code-$version-x86_64.AppImage" '$2 == f {print $1}' <<<"$sums")
        cli=$(awk -v f="t3-$version-linux-x64.tar.gz" '$2 == f {print $1}' <<<"$sums")
        if [ -z "$appimage" ] || [ -z "$cli" ]; then
          pause "release $tag lacks the AppImage or the CLI tarball"
          start_thread "Z3 Code $version: release incomplete" "$(debug_prompt "release $tag has no AppImage or CLI tarball in SHA256SUMS" "$log" "SHA256SUMS: $sums")"
          return 1
        fi
        appimage=$(nix hash convert --hash-algo sha256 --to sri "$appimage")
        cli=$(nix hash convert --hash-algo sha256 --to sri "$cli")
        (
          cd "$clone"
          sed -i "s|^  version = \".*\";$|  version = \"$version\";|" "''${packages[@]}"
          sed -i "s|^    hash = \"sha256-.*\";$|    hash = \"$appimage\";|" cells/common/packages/z3code.nix
          sed -i "s|^    hash = \"sha256-.*\";$|    hash = \"$cli\";|" cells/common/packages/t3-cli.nix
          git diff --stat
        ) >>"$log" 2>&1

        if ! (
          cd "$clone"
          nice -n 10 nix build --no-link --keep-going \
            .#nixosConfigurations.functor.config.system.build.toplevel \
            .#nixosConfigurations.isomorph.config.system.build.toplevel
        ) >>"$log" 2>&1; then
          git -C "$clone" diff >"$state/bump-$version.diff"
          pause "the systems don't build with $version"
          start_thread "Z3 Code $version doesn't build" "$(debug_prompt "building functor and isomorph with $version pinned failed; nothing was pushed" "$log" "The pin change is in $state/bump-$version.diff (applied, unpushed, in the clone $clone).")"
          return 1
        fi

        git -C "$clone" commit --quiet -m "z3code: $version" -- "''${packages[@]}"
        # main may have moved while the systems built: rebase once and retry.
        if ! push "$clone" >>"$log" 2>&1; then
          if ! git -C "$clone" pull --quiet --rebase origin main >>"$log" 2>&1 || ! push "$clone" >>"$log" 2>&1; then
            record "push of $version failed; the next run tries again"
            return 1
          fi
        fi
        record "pushed z3code: $version"
        systemctl start --no-block nixos-ci.service 2>/dev/null || true
      }

      status() {
        local want
        echo "host        $host"
        echo "running     $(running)"
        want=$(origin_version 2>/dev/null || true)
        echo "origin/main $want"
        if paused; then sed 's/^/paused      /' "$state/paused"; else echo "paused      no"; fi
        [ -e "$state/waiting" ] && echo "waiting     $(cat "$state/waiting")"
        echo "busy now    $(busy_reason || true)"
        echo
        tail -n 8 "$state/history" 2>/dev/null || true
        echo
        systemctl --user list-timers --no-pager --all z3-autoupdate-apply.timer z3-autoupdate-bump.timer 2>/dev/null || true
      }

      case ''${1:-status} in
        status) status ;;
        apply)
          if [ "''${2:-}" = --force ]; then apply 1; else apply 0; fi
          ;;
        bump) bump ;;
        pause)
          pause "''${2:-paused by hand}"
          ;;
        resume)
          # Applying restarts the T3 server (or the desktop app), which would cut off a thread that ran this; the
          # apply unit instead waits for the host to be idle, as usual.
          rm -f "$state/paused"
          record "resumed"
          if [ "$bump_host" = true ]; then bump || true; fi
          systemctl --user start --no-block z3-autoupdate-apply.service
          say "apply runs once $host is idle (z3-autoupdate status); z3-autoupdate apply --force applies now"
          ;;
        log)
          shift
          journalctl --user --no-pager --lines 200 -u z3-autoupdate-apply.service -u z3-autoupdate-bump.service "$@"
          ;;
        *)
          echo "usage: z3-autoupdate [status|apply [--force]|bump|pause [reason]|resume|log [-f]]" >&2
          exit 2
          ;;
      esac
    '';
  };

  unitPath = "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin:%h/.nix-profile/bin";
  oneshot = description: args: {
    inherit description;
    unitConfig.ConditionUser = user;
    # A switch must not restart (and so kill) the run that started it.
    restartIfChanged = false;
    environment.PATH = lib.mkForce unitPath;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${lib.getExe cli} ${args}";
      Nice = 5;
    };
  };
in {
  options.z3-autoupdate = {
    bump = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "This host pins each new Z3 Code release on main (one host only: functor).";
    };
    restartUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "User units to restart after a switch, such as the T3 server.";
    };
    desktopApp = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Relaunch the Z3 Code desktop app after a switch if it was running.";
    };
  };

  config = {
    environment.systemPackages = [cli];

    systemd.user.services = {
      z3-autoupdate-apply = oneshot "Apply the Z3 Code version main pins, when idle" "apply";
      # Marks the session idle after 15 minutes without input, for apply's gate.
      z3-autoupdate-idle = {
        description = "Tell z3-autoupdate when this session has been idle for 15 minutes";
        unitConfig.ConditionUser = user;
        partOf = ["graphical-session.target"];
        after = ["graphical-session.target"];
        wantedBy = ["graphical-session.target"];
        serviceConfig = {
          ExecStartPre = [
            "${pkgs.coreutils}/bin/mkdir -p %t/z3-autoupdate"
            "${pkgs.coreutils}/bin/rm -f %t/z3-autoupdate/idle"
          ];
          ExecStart = lib.escapeShellArgs [
            "${pkgs.swayidle}/bin/swayidle"
            "timeout"
            "900"
            "${pkgs.coreutils}/bin/touch %t/z3-autoupdate/idle"
            "resume"
            "${pkgs.coreutils}/bin/rm -f %t/z3-autoupdate/idle"
          ];
          Restart = "on-failure";
        };
      };
      z3-autoupdate-bump = lib.mkIf cfg.bump (oneshot "Pin the newest Z3 Code release on main" "bump");
    };

    systemd.user.timers = {
      z3-autoupdate-apply = {
        unitConfig.ConditionUser = user;
        wantedBy = ["timers.target"];
        timerConfig = {
          OnBootSec = "10min";
          OnUnitActiveSec = "15min";
        };
      };
      z3-autoupdate-bump = lib.mkIf cfg.bump {
        unitConfig.ConditionUser = user;
        wantedBy = ["timers.target"];
        timerConfig = {
          OnCalendar = "*:20";
          Persistent = true;
        };
      };
    };
  };
}
