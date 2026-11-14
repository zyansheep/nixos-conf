# nixos-deploy: keep each host on the newest commit of main that nixos-ci built for it, the way dither-chat's
# deployer does: only green commits, when the host is idle, checked after the switch, rolled back if a check fails.
#
# Every 15 minutes (a user service of zyansheep's):
#   1. The commit this host runs (`nixos-version --configuration-revision`) must be on main. The candidates are the
#      commits on main after it, newest first; it takes the first that nixos-ci built for this host
#      (`nixos-ci results` on functor) and that hasn't failed here before.
#   2. It waits until the host is idle: no T3 agent run in progress, nobody at the machine for 15 minutes or the
#      screen locked, on AC power, no builds (`nixos-deploy busy` says why not).
#   3. It switches:
#      - running a clean commit: to nixos-ci's store path, through update-manager-switch@ (fetched from functor's
#        cache with signatures checked). ~/nixos-conf is fast-forwarded too when it can be, but needn't be.
#      - running a build of uncommitted changes (`nrb` from a dirty tree, "<rev>-dirty"): fast-forwards
#        ~/nixos-conf to the commit and runs `sudo rebuild`, so the local changes stay, as with `nrb`. If the tree
#        can't fast-forward, it waits (and says so once).
#   4. Checks: no unit failed that hadn't before, every unit in nixos-deploy.checkUnits that was active still is,
#      and each nixos-deploy.postSwitch command (z3-autoupdate's restarts Z3 Code) prints nothing and succeeds.
#   5. On a failed check it switches back to the previous system, runs the postSwitch commands again, marks the
#      commit bad (never tried again on this host), pauses, and starts a T3 thread in nixos-conf to find out why.
#
#   nixos-deploy status           what runs, what would deploy, gates, pause, history
#   nixos-deploy plan             what a run would deploy now, without the gates or switching
#   nixos-deploy apply [--force]  run now (--force skips the idle gates)
#   nixos-deploy pause [reason] / resume
#   nixos-deploy hold / unhold    keep this host where it is until unhold (a pause that starts no thread)
#   nixos-deploy busy             why this host isn't idle, or nothing
#   nixos-deploy log [-f]         the unit's journal
#
# `rebuild` (core.rebuild) refuses to switch to a tree that doesn't contain the running commit, so a stale
# ~/nixos-conf can't undo a deploy.
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.nixos-deploy;
  user = "zyansheep";
  onCiHost = config.networking.hostName == cfg.ciHost;

  cli = pkgs.writeShellApplication {
    name = "nixos-deploy";
    runtimeInputs = with pkgs; [
      bash
      coreutils
      gawk
      git
      gnugrep
      jq
      libnotify
      openssh
      procps
      sqlite
      systemd
      util-linux
    ];
    text = ''
      conf=$HOME/nixos-conf
      url=https://github.com/zyansheep/nixos-conf.git
      state=''${XDG_STATE_HOME:-$HOME/.local/state}/nixos-deploy
      runtime=''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/nixos-deploy
      host=${config.networking.hostName}
      ci_host=${cfg.ciHost}
      on_ci_host=${lib.boolToString onCiHost}
      check_units=(${lib.escapeShellArgs cfg.checkUnits})
      post_switch=(${lib.escapeShellArgs cfg.postSwitch})
      t3_db=$HOME/.t3/userdata/statev2.sqlite
      clone=$state/main.git
      mkdir -p "$state/bad" "$runtime"

      say() { echo "nixos-deploy: $*" >&2; }
      record() { printf '%s %s\n' "$(date -Is)" "$*" >>"$state/history"; say "$*"; }
      notify() { notify-send --app-name "nixos-deploy" "$1" "''${2:-}" 2>/dev/null || true; }
      wait_for() {
        echo "$(date -Is) $1" >"$state/waiting"
        say "$1"
      }
      # Says $2 once per key $1 (a commit), in the history and as a notification.
      tell_once() {
        [ -e "$state/told-$1" ] && return 0
        touch "$state/told-$1"
        record "$2"
        notify "Update waiting" "$2"
      }

      paused() { [ -e "$state/paused" ]; }
      pause() {
        { echo "since   $(date -Is)"; echo "reason  $1"; } >"$state/paused"
        record "paused: $1"
      }

      # --- idle gates -------------------------------------------------------------------------------------------
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

      # --- what to deploy ---------------------------------------------------------------------------------------
      ci_results() {
        if [ "$on_ci_host" = true ]; then
          nixos-ci results "$@"
        else
          ssh -o BatchMode=yes -o ConnectTimeout=5 "$ci_host" nixos-ci results "$@"
        fi
      }

      # Sets running (the commit this host runs), dirty (1 if it was built from uncommitted changes), candidate
      # and candidate_path (the commit to deploy and nixos-ci's system for it); or sets why (there's nothing to
      # deploy) and returns 1. Not in $(...): the caller needs the variables.
      plan() {
        local rev revs results path tip tip_result
        why=
        running=$(nixos-version --configuration-revision 2>/dev/null || true)
        dirty=0
        if [[ $running == *-dirty ]]; then dirty=1; running=''${running%-dirty}; fi
        if ! [[ $running =~ ^[0-9a-f]{40}$ ]]; then
          why="the running system doesn't record its commit ('$running'): switch once with nrb"
          return 1
        fi
        [ -d "$clone" ] || git clone --quiet --bare --filter=blob:none "$url" "$clone"
        git -C "$clone" fetch --quiet origin +refs/heads/main:refs/heads/main || {
          why="could not fetch main"
          return 1
        }
        if ! git -C "$clone" merge-base --is-ancestor "$running" main 2>/dev/null; then
          why="the running commit ''${running:0:12} isn't on main (a branch, or main was rewritten): nrb from main to rejoin"
          return 1
        fi
        mapfile -t revs < <(git -C "$clone" rev-list --first-parent --max-count 50 "$running..main")
        [ ''${#revs[@]} -gt 0 ] || { why="up to date with main (''${running:0:12})"; return 1; }
        tip=''${revs[0]}
        results=$(ci_results "''${revs[@]}") || { why="could not ask $ci_host for nixos-ci's results"; return 1; }
        candidate=
        candidate_path=
        for rev in "''${revs[@]}"; do
          [ -e "$state/bad/$rev" ] && continue
          path=$(awk -v rev="$rev" -v host="$host" '
            $1 == "rev" { cur = $2; next }
            cur == rev && $1 == host && $2 == "ok" { print $3 }' <<<"$results")
          if [ -n "$path" ]; then
            candidate=$rev
            candidate_path=$path
            break
          fi
        done
        if [ -z "$candidate" ]; then
          tip_result=$(awk -v rev="$tip" -v host="$host" '$1 == "rev" { cur = $2; next } cur == rev && $1 == host { print $2 }' <<<"$results")
          case $tip_result in
            FAILED) why="main (''${tip:0:12}) doesn't build for $host, and no commit between it and ''${running:0:12} did" ;;
            *) why="nixos-ci hasn't built a newer commit for $host yet (main is ''${tip:0:12})" ;;
          esac
          return 1
        fi
        return 0
      }

      # --- checks after a switch --------------------------------------------------------------------------------
      failed_units() {
        { systemctl --failed --no-legend --plain 2>/dev/null; systemctl --user --failed --no-legend --plain 2>/dev/null; } |
          awk '{print $1}' | sort -u
      }
      active_check_units() {
        local unit
        for unit in "''${check_units[@]}"; do
          systemctl --user is-active --quiet "$unit" && echo "$unit"
        done
        return 0
      }
      run_post_switch() {
        local cmd out
        for cmd in "''${post_switch[@]}"; do
          # What it says on stderr is its log, not a problem.
          if ! out=$(bash -c "$cmd" 2>>"$state/post-switch.log") || [ -n "$out" ]; then
            echo "$cmd: ''${out:-exited non-zero}"
          fi
        done
        return 0
      }
      # Prints what is wrong after the switch to $1, or nothing. $2/$3: failed units and active check units before.
      health() {
        local want=$1 failed_before=$2 active_before=$3 unit new problems
        [ "$(readlink -f /run/current-system)" = "$want" ] || { echo "/run/current-system is not $want"; return; }
        # Let restarted units settle before judging them.
        sleep 20
        problems=$(run_post_switch)
        [ -z "$problems" ] || { echo "$problems"; return; }
        while read -r unit; do
          [ -z "$unit" ] || systemctl --user is-active --quiet "$unit" || { echo "$unit is no longer active"; return; }
        done <<<"$active_before"
        new=$(comm -13 <(echo "$failed_before") <(failed_units))
        if [ -n "$new" ]; then echo "units failed after the switch: $(echo "$new" | tr '\n' ' ')"; fi
        return 0
      }

      switch_unit() {
        # The instance is the escaped store name: a bare "-" would decode to "/".
        echo "update-manager-switch@$(systemd-escape "$(basename "$1")").service"
      }

      # --- the T3 debug thread ------------------------------------------------------------------------------------
      start_thread() {
        local title=$1 body=$2 out link
        t3 project add "$conf" >/dev/null 2>&1 || true
        if out=$(printf '%s\n' "$body" | t3 thread start "$conf" - --title "$title" --json 2>>"$state/thread.err"); then
          link=$(jq -r '.link // .threadId // empty' <<<"$out" 2>/dev/null || true)
          echo "thread  ''${link:-$out}" >>"$state/paused"
          record "debug thread: ''${link:-$out}"
          notify "$title" "A T3 thread is looking into it: ''${link:-see nixos-deploy status}"
        else
          record "could not start the debug thread (see $state/thread.err)"
          notify "$title" "Paused. Could not start a T3 thread; see nixos-deploy status."
        fi
      }

      debug_prompt() {
        # $1 what failed, $2 log file, $3 what was done about it
        cat <<EOF
      nixos-deploy on $host switched to commit $candidate of nixos-conf and a check failed. Find out why.

      What happened: $1
      $3

      Deploys on $host are paused (\`nixos-deploy status\`; its state is in $state, the log of this attempt is
      $2, and \`nixos-deploy log\` shows the unit's journal), and $candidate is marked bad on $host, so it is
      never tried again; a later commit on main is. How it works is in
      ~/nixos-conf/cells/nixos/profiles/services/nixos-deploy.nix.

      Last lines of the log:
      $(tail -n 60 "$2" 2>/dev/null)

      If the cause is clear and the fix is small and safe, fix it: work in a fresh worktree of nixos-conf off
      origin/main (never in ~/nixos-conf, which may hold uncommitted work), build the host
      (\`nix build --no-link .#nixosConfigurations.$host.config.system.build.toplevel\`), commit and push to main,
      then run \`nixos-deploy resume\` on $host: it deploys once nixos-ci has built the fix and the host is idle,
      after this thread's turn ends. Don't run \`nixos-deploy apply --force\` yourself: a switch can restart the T3
      server this thread runs on.

      If it isn't clear, or the fix is risky, change nothing: leave deploys paused, and end with what broke, the
      evidence, and the options, for the user to review.
      EOF
      }

      # --- apply --------------------------------------------------------------------------------------------------
      apply() {
        local force=$1 reason prev log failed_before active_before problem="" want running dirty candidate candidate_path why
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        if paused; then say "paused ($(sed -n 's/^reason  //p' "$state/paused"))"; return 0; fi

        if ! plan; then
          wait_for "$why"
          return 0
        fi
        if [ "$force" != 1 ]; then
          reason=$(busy_reason || true)
          if [ -n "$reason" ]; then
            wait_for "waiting to deploy ''${candidate:0:12}: $reason"
            return 0
          fi
        fi
        if [ "$dirty" = 1 ]; then
          git -C "$conf" fetch --quiet origin main || true
          if [ "$(git -C "$conf" symbolic-ref --short HEAD 2>/dev/null)" != main ] ||
            ! git -C "$conf" merge --ff-only --quiet "$candidate" >/dev/null 2>&1; then
            # Local commits or edits in the way: wait, so it goes through once the tree is pushed or synced.
            reason="$host runs uncommitted changes from $conf, which can't fast-forward to ''${candidate:0:12} (not on main, local commits, or local edits to the same files)"
            wait_for "$reason"
            tell_once "$candidate" "$reason"
            return 0
          fi
          git -C "$conf" submodule --quiet update --init --recursive >/dev/null 2>&1 || true
        fi
        rm -f "''${state:?}/waiting"

        log=$state/deploy-''${candidate:0:12}.log
        : >"$log"
        prev=$(readlink -f /run/current-system)
        failed_before=$(failed_units)
        active_before=$(active_check_units)
        if [ "$dirty" = 1 ]; then
          record "deploying ''${candidate:0:12} by rebuilding $conf (it runs uncommitted changes on ''${running:0:12})"
          # The log is ours to write; only rebuild runs as root.
          # shellcheck disable=SC2024
          if ! /run/wrappers/bin/sudo -n /run/current-system/sw/bin/rebuild >>"$log" 2>&1; then
            if grep -q 'retry when .* answers, or pass --allow-local-builds' "$log"; then
              record "rebuild deferred: functor's cache did not answer"
              return 0
            fi
            problem="sudo rebuild failed"
          fi
        else
          record "deploying ''${candidate:0:12} ($candidate_path, running ''${running:0:12})"
          if ! systemctl start "$(switch_unit "$candidate_path")" >>"$log" 2>&1; then
            problem="the switch to $candidate_path failed ($(switch_unit "$candidate_path"))"
          fi
          # The tree follows when it can; it doesn't have to.
          if [ "$(git -C "$conf" symbolic-ref --short HEAD 2>/dev/null)" = main ]; then
            git -C "$conf" fetch --quiet origin main >/dev/null 2>&1 &&
              git -C "$conf" merge --ff-only --quiet "$candidate" >/dev/null 2>&1 || true
          fi
        fi
        if [ -z "$problem" ]; then
          # A rebuild of uncommitted changes makes a system nobody knew in advance.
          want=$candidate_path
          [ "$dirty" = 1 ] && want=$(readlink -f /run/current-system)
          problem=$(health "$want" "$failed_before" "$active_before" 2>&1 | tee -a "$log" || true)
        fi
        if [ -z "$problem" ]; then
          record "deployed ''${candidate:0:12}"
          notify "System updated" "$host runs ''${candidate:0:12} of nixos-conf now"
          return 0
        fi

        record "deploying ''${candidate:0:12} failed: $problem"
        touch "$state/bad/$candidate"
        local rolled="Nothing was switched."
        if [ "$(readlink -f /run/current-system)" != "$prev" ]; then
          if systemctl start "$(switch_unit "$prev")" >>"$log" 2>&1; then
            run_post_switch >>"$log" 2>&1
            rolled="It was switched back to the previous system ($prev)."
          else
            rolled="Switching back to $prev FAILED ($(switch_unit "$prev")); the new system is still active."
          fi
        fi
        record "$rolled"
        pause "deploying ''${candidate:0:12} failed: $problem"
        start_thread "nixos-conf ''${candidate:0:12} failed on $host" "$(debug_prompt "$problem" "$log" "$rolled")"
        return 1
      }

      status() {
        local running dirty candidate candidate_path why
        echo "host        $host"
        echo "running     $(nixos-version --configuration-revision 2>/dev/null || echo unknown)"
        if plan; then echo "next        $candidate $candidate_path"; else echo "next        none: $why"; fi
        if paused; then sed 's/^/paused      /' "$state/paused"; else echo "paused      no"; fi
        [ -e "$state/waiting" ] && echo "waiting     $(cat "$state/waiting")"
        echo "busy now    $(busy_reason || true)"
        echo
        tail -n 8 "$state/history" 2>/dev/null || true
        echo
        systemctl --user list-timers --no-pager --all nixos-deploy.timer 2>/dev/null || true
      }

      case ''${1:-status} in
        status) status ;;
        plan)
          if plan; then echo "would deploy $candidate: $candidate_path"; else echo "$why"; fi
          ;;
        apply)
          if [ "''${2:-}" = --force ]; then apply 1; else apply 0; fi
          ;;
        busy) busy_reason || true ;;
        pause) pause "''${2:-paused by hand}" ;;
        hold) pause "held by hand (nixos-deploy unhold)" ;;
        resume | unhold)
          rm -f "''${state:?}/paused"
          record "resumed"
          systemctl --user start --no-block nixos-deploy.service
          say "deploys once $host is idle (nixos-deploy status); nixos-deploy apply --force deploys now"
          ;;
        log)
          shift
          journalctl --user --no-pager --lines 200 -u nixos-deploy.service "$@"
          ;;
        *)
          echo "usage: nixos-deploy [status|plan|apply [--force]|busy|pause [reason]|resume|hold|unhold|log [-f]]" >&2
          exit 2
          ;;
      esac
    '';
  };
in {
  options.nixos-deploy = {
    ciHost = lib.mkOption {
      type = lib.types.str;
      default = "functor";
      description = "The host that runs nixos-ci (reached as `ssh <ciHost>` from the others).";
    };
    checkUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "User units that must still be active after a switch if they were active before it.";
    };
    postSwitch = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = ''
        Commands run (as the user) after a switch and after a switch back. Each prints what is wrong, or
        nothing; output or a non-zero exit fails the deploy.
      '';
    };
  };

  config = {
    environment.systemPackages = [cli];

    systemd.user.services = {
      nixos-deploy = {
        description = "Deploy the newest commit of nixos-conf that nixos-ci built, when idle";
        unitConfig.ConditionUser = user;
        # A switch must not restart (and so kill) the run that started it.
        restartIfChanged = false;
        environment.PATH = lib.mkForce "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin:%h/.nix-profile/bin";
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${lib.getExe cli} apply";
          Nice = 5;
        };
      };
      # Marks the session idle after 15 minutes without input, for the idle gate.
      nixos-deploy-idle = {
        description = "Tell nixos-deploy when this session has been idle for 15 minutes";
        unitConfig.ConditionUser = user;
        partOf = ["graphical-session.target"];
        after = ["graphical-session.target"];
        wantedBy = ["graphical-session.target"];
        serviceConfig = {
          ExecStartPre = [
            "${pkgs.coreutils}/bin/mkdir -p %t/nixos-deploy"
            "${pkgs.coreutils}/bin/rm -f %t/nixos-deploy/idle"
          ];
          ExecStart = lib.escapeShellArgs [
            "${pkgs.swayidle}/bin/swayidle"
            "timeout"
            "900"
            "${pkgs.coreutils}/bin/touch %t/nixos-deploy/idle"
            "resume"
            "${pkgs.coreutils}/bin/rm -f %t/nixos-deploy/idle"
          ];
          Restart = "on-failure";
        };
      };
    };

    systemd.user.timers.nixos-deploy = {
      unitConfig.ConditionUser = user;
      wantedBy = ["timers.target"];
      timerConfig = {
        OnBootSec = "10min";
        OnUnitActiveSec = "15min";
      };
    };
  };
}
