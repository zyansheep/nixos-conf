# z3-autoupdate: keep Z3 Code (the zyansheep/t3code fork, cells/common/packages/z3code.nix and t3-cli.nix) on
# the newest release, which follows upstream's nightly.
#
# - bump (functor, hourly; z3-autoupdate.bump): in its own clone of origin/main (never ~/nixos-conf), points the
#   two packages at the newest release, builds both hosts' systems, and commits "z3code: <version>" and pushes it
#   to main. nixos-ci then caches the systems for isomorph.
#   A build that fails is tried again on the next two runs (a build killed by a switch or a daemon restart is no
#   verdict on the release); the third failure pauses bump and starts a T3 thread. A newer release ends that pause.
# - Switching the system is nixos-deploy's (services.nixos-deploy): it deploys the bump like any other commit once
#   nixos-ci has built it. A switch restarts nothing that runs Z3 Code; its hook `z3-autoupdate after-switch` only
#   wakes apply.
# - apply (every host, every minute): when the profile has a newer Z3 Code than what runs, restarts what runs it
#   (z3-autoupdate.restartUnits, the desktop app) once no agent run would be cut off and nobody is using the machine
#   (`nixos-deploy busy --restart`), and checks it. A host that has waited a day restarts anyway inside
#   z3-autoupdate.restartWindow, if it has one (functor: agents there never stop for long).
#   Lockstep: the desktop app only moves to a version the CI host's server (functor) already runs, since the
#   desktop refuses a server on another orchestration protocol; and when functor's server speaks another protocol
#   than this desktop, the desktop restarts even with someone at the machine (agent runs still hold it).
#   On a failed check it pauses itself and starts a T3 thread in the nixos-conf project on this host to find out
#   why. That thread fixes what is plainly fixable and runs `z3-autoupdate resume`; otherwise it leaves the update
#   paused for review.
#
#   z3-autoupdate status           versions, gates, pause, last result
#   z3-autoupdate apply [--force]  restart onto the profile's version now (--force skips the idle gates)
#   z3-autoupdate after-switch     nixos-deploy's hook: restart if needed, print what's wrong
#   z3-autoupdate bump             (functor) check for a new release now
#   z3-autoupdate pause [reason] / resume
#   z3-autoupdate log [-f]         the units' journal
#
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
      runtime_json=$HOME/.t3/userdata/server-runtime.json
      ci_host=${config.nixos-deploy.ciHost}
      restart_window=${lib.escapeShellArg (toString cfg.restartWindow)}
      alert_command=${lib.escapeShellArg (toString config.nixos-deploy.alertCommand)}
      thread_args=(--provider ${lib.escapeShellArg config.nixos-deploy.thread.provider} --model ${lib.escapeShellArg config.nixos-deploy.thread.model})
      packages=(cells/common/packages/z3code.nix cells/common/packages/t3-cli.nix)
      mkdir -p "$state" "$runtime"

      say() { echo "z3-autoupdate: $*" >&2; }
      record() { printf '%s %s\n' "$(date -Is)" "$*" >>"$state/history"; say "$*"; }
      # $1 title, $2 body, $3 urgency: a notification here, and nixos-deploy.alertCommand (wherever the user is).
      notify() {
        notify-send --app-name "Z3 Code update" --urgency "''${3:-normal}" "$1" "''${2:-}" 2>/dev/null || true
        if [ -n "$alert_command" ]; then
          bash -c "$alert_command" alert "$1" "''${2:-}" "''${3:-normal}" >>"$state/alert.log" 2>&1 || true
        fi
      }
      # Says $1 in the journal and the waiting file, once per change.
      wait_for() {
        [ "$(cut -d' ' -f2- "$state/waiting" 2>/dev/null)" = "$1" ] && return 0
        echo "$(date -Is) $1" >"$state/waiting"
        say "$1"
      }
      # True if version $1 is $2 or newer.
      version_ge() { [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]; }
      # The T3 server at $1 (host:port): "<version> <orchestration protocol>", or nothing.
      environment_of() {
        curl -fsS --max-time 5 "http://$1/.well-known/t3/environment" 2>/dev/null |
          jq -r '"\(.serverVersion // "") \(.orchestrationProtocolVersion // "")"' 2>/dev/null || true
      }

      # The version a nixos-conf tree pins, from t3-cli.nix (z3code.nix pins the same).
      pinned() { sed -n 's/^ *version = "\(.*\)";$/\1/p' "$1"; }
      # The version this host's profile runs, from the t3 CLI's store path.
      running() {
        local t3
        t3=$(readlink -f "/etc/profiles/per-user/$USER/bin/t3" 2>/dev/null || true)
        sed -n 's|^/nix/store/[^/]*-t3-cli-\(.*\)/bin/t3$|\1|p' <<<"$t3"
      }
      origin_version() {
        git -C "$conf" fetch --quiet origin main
        git -C "$conf" show origin/main:cells/common/packages/t3-cli.nix | sed -n 's/^ *version = "\(.*\)";$/\1/p'
      }

      # apply's pause ($state/paused) and bump's ($state/paused-bump, which names the release it failed on).
      paused() { [ -e "$state/paused" ]; }
      pause() {
        { echo "since   $(date -Is)"; echo "reason  $1"; } >"$state/paused"
        record "paused: $1"
      }
      pause_bump() {
        { echo "since   $(date -Is)"; echo "version $1"; echo "reason  $2"; } >"$state/paused-bump"
        record "bump paused: $2"
      }

      # --- restart gates: nixos-deploy's ------------------------------------------------------------------------
      # Prints why a restart should wait, or nothing.
      busy_reason() { nixos-deploy busy --restart; }

      # --- the T3 debug thread ----------------------------------------------------------------------------------
      start_thread() {
        local title=$1 body=$2 out link
        t3 project add "$conf" >/dev/null 2>&1 || true
        if out=$(printf '%s\n' "$body" | t3 thread start "$conf" - --title "$title" "''${thread_args[@]}" --json 2>>"$state/thread.err"); then
          link=$(jq -r '.link // .threadId // empty' <<<"$out" 2>/dev/null || true)
          record "debug thread: ''${link:-$out}"
          notify "$title" "Paused. A T3 thread on $host is looking into it: ''${link:-see z3-autoupdate status}" critical
        else
          record "could not start the debug thread (see $state/thread.err)"
          notify "$title" "Paused. Could not start a T3 thread; see z3-autoupdate status on $host." critical
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
      zyansheep/t3code in cells/common/packages/z3code.nix and t3-cli.nix and pushes to main; nixos-deploy (each
      host, cells/nixos/profiles/services/nixos-deploy.nix) switches to it once nixos-ci has built it, then
      z3-autoupdate restarts Z3 Code and checks it.

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
        local force=$1 want have reason log before problem urgent=0 ci_env ci_version ci_protocol own_protocol since
        exec 9>"$runtime/apply.lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        if paused; then say "paused ($(sed -n 's/^reason  //p' "$state/paused"))"; return 0; fi

        # The profile holds the version to run (switching to it is nixos-deploy's); what runs may lag behind it.
        want=$(running)
        have=$(server_version)
        [ -n "$want" ] || { wait_for "no t3 in the profile"; return 0; }
        if [ -z "$have" ] || [ "$have" = "$want" ]; then rm -f "''${state:?}/waiting" "''${state:?}/waiting-since"; return 0; fi
        # Never downgrade: a profile older than what runs (a hand rebuild ran ahead of main) waits for a newer one.
        if version_ge "$have" "$want"; then
          wait_for "the profile has $want, older than the running $have; nothing to do"
          return 0
        fi
        # Lockstep: the desktop app moves only to a version the CI host's server already runs.
        if [ "$desktop_app" = true ] && [ "$host" != "$ci_host" ]; then
          ci_env=$(environment_of "$ci_host:3773")
          if [ -z "$ci_env" ] && [ "$force" != 1 ]; then
            wait_for "waiting to restart on $want: can't reach $ci_host's T3 server to check it runs $want first"
            return 0
          fi
          if [ -n "$ci_env" ]; then
            ci_version=''${ci_env% *}
            ci_protocol=''${ci_env#* }
            if [ "$force" != 1 ] && ! version_ge "$ci_version" "$want"; then
              wait_for "waiting for $ci_host's T3 server to run $want first (it runs $ci_version)"
              return 0
            fi
            own_protocol=$(environment_of "127.0.0.1:3773")
            own_protocol=''${own_protocol#* }
            # This desktop can't talk to the CI host's server until it restarts: someone at the machine doesn't hold it.
            [ -n "$own_protocol" ] && [ -n "$ci_protocol" ] && [ "$own_protocol" != "$ci_protocol" ] && urgent=1
          fi
        fi
        if [ "$force" != 1 ]; then
          reason=$(busy_reason || true)
          if [ "$urgent" = 1 ] && [[ $reason == "someone is at the machine"* ]]; then
            notify "Restarting Z3 Code" "$ci_host's T3 server speaks orchestration protocol $ci_protocol and this desktop $own_protocol: restarting it on $want." critical
            reason=
          fi
          if [ -n "$reason" ]; then
            # One clock for the whole wait, however many releases come out meanwhile.
            [ -e "$state/waiting-since" ] || date +%s >"$state/waiting-since"
            since=$(cat "$state/waiting-since")
            # A host whose agents never stop for long restarts anyway after a day, inside its window.
            if [ -n "$restart_window" ] && [ $(($(date +%s) - since)) -gt 86400 ] &&
              [ "$(date +%-H)" -ge "''${restart_window%-*}" ] && [ "$(date +%-H)" -lt "''${restart_window#*-}" ]; then
              record "restarting on $want although $reason: waited since $(date -d "@$since" -Is), inside the $restart_window h window"
            else
              wait_for "waiting to restart on $want: $reason"
              return 0
            fi
          fi
        fi
        rm -f "''${state:?}/waiting" "''${state:?}/waiting-since"

        log=$state/apply-$want.log
        : >>"$log"
        before=$(failed_units)
        rm -f "''${runtime:?}/desktop-was-running"
        if [ "$desktop_app" = true ] && [ -n "$(desktop_pid || true)" ]; then touch "$runtime/desktop-was-running"; fi
        record "restarting Z3 Code on $want (it runs $have)"
        restart_z3
        problem=$(health "$want" "$before" 2>&1 | tee -a "$log" || true)
        if [ -z "$problem" ]; then record "restarted on $want"; return 0; fi
        record "restart on $want failed: $problem"
        pause "restarting on $want failed: $problem"
        start_thread "Z3 Code $want failed to start on $host" "$(debug_prompt "restarting Z3 Code on $want (it ran $have): $problem" "$log" "The profile already had $want; nixos-deploy switched to it earlier.")"
        return 1
      }

      # nixos-deploy's postSwitch hook (and its hook after a switch back). A switch doesn't restart Z3 Code: apply does,
      # on its own gates, so this only wakes it. It prints nothing (a deploy fails on output).
      after_switch() {
        systemctl --user start --no-block z3-autoupdate-apply.service 2>/dev/null || true
      }

      # --- bump (functor) -----------------------------------------------------------------------------------------
      push() {
        git -C "$1" -c credential.helper= -c credential.helper='!gh auth git-credential' push --quiet origin main:main
      }

      # A failed bump of $1 ($2 what failed, $3 the log, $4 more context): tried again on the next two runs, then a
      # pause and a debug thread.
      bump_failed() {
        local tries
        tries=$(($(cat "$state/bump-tries-$1" 2>/dev/null || echo 0) + 1))
        echo "$tries" >"$state/bump-tries-$1"
        if [ "$tries" -lt 3 ]; then
          record "bump to $1 failed ($2); trying again next run ($tries of 3)"
          return 1
        fi
        pause_bump "$1" "$2 (3 tries)"
        start_thread "Z3 Code $1: $2" "$(debug_prompt "bumping to $1: $2, three runs in a row; nothing was pushed" "$3" "$4")"
        return 1
      }

      bump() {
        local tag version clone log sums appimage cli current
        [ "$bump_host" = true ] || { say "this host doesn't bump"; return 0; }
        exec 8>"$runtime/bump.lock"
        flock -n 8 || { say "another bump is in progress"; return 0; }

        tag=$(gh api repos/zyansheep/t3code/releases/latest -q .tag_name)
        [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+-.*-z3\.[0-9]+$ ]] || { say "latest release '$tag' is not a z3 release"; return 0; }
        version=''${tag#v}
        if [ -e "$state/paused-bump" ]; then
          if [ "$(sed -n 's/^version //p' "$state/paused-bump")" = "$version" ]; then say "bump paused on $version"; return 0; fi
          rm -f "$state/paused-bump"
          record "bump unpaused: a newer release ($version) is out"
        fi

        clone=$state/nixos-conf
        [ -d "$clone/.git" ] || git clone --quiet https://github.com/zyansheep/nixos-conf.git "$clone"
        git -C "$clone" fetch --quiet origin main
        git -C "$clone" checkout --quiet --force -B main origin/main
        git -C "$clone" clean --quiet -ffdx
        git -C "$clone" submodule --quiet sync --recursive
        git -C "$clone" submodule --quiet update --init --recursive --force
        current=$(pinned "$clone/cells/common/packages/t3-cli.nix")
        if [ "$current" = "$version" ]; then say "main already pins $version"; return 0; fi
        if version_ge "$current" "$version"; then say "main pins $current, newer than the latest release $version"; return 0; fi

        log=$state/bump-$version.log
        : >>"$log"
        record "bumping $current -> $version"
        sums=$(curl -fsSL "$repo/releases/download/$tag/SHA256SUMS" || true)
        appimage=$(awk -v f="Z3-Code-$version-x86_64.AppImage" '$2 == f {print $1}' <<<"$sums")
        cli=$(awk -v f="t3-$version-linux-x64.tar.gz" '$2 == f {print $1}' <<<"$sums")
        if [ -z "$appimage" ] || [ -z "$cli" ]; then
          # The release may still be uploading its assets.
          bump_failed "$version" "release $tag lacks the AppImage or the CLI tarball" "$log" "SHA256SUMS: $sums"
          return 1
        fi
        appimage=$(nix hash convert --hash-algo sha256 --to sri "$appimage")
        cli=$(nix hash convert --hash-algo sha256 --to sri "$cli")
        (
          cd "$clone"
          # Each file has one version and one hash line; indentation is whatever the formatter left.
          sed -i "s|^\( *\)version = \".*\";$|\1version = \"$version\";|" "''${packages[@]}"
          sed -i "s|^\( *\)hash = \"sha256-.*\";$|\1hash = \"$appimage\";|" cells/common/packages/z3code.nix
          sed -i "s|^\( *\)hash = \"sha256-.*\";$|\1hash = \"$cli\";|" cells/common/packages/t3-cli.nix
          git diff --stat
        ) >>"$log" 2>&1

        if ! (
          cd "$clone"
          nice -n 10 nix build --no-link --keep-going \
            .#nixosConfigurations.functor.config.system.build.toplevel \
            .#nixosConfigurations.isomorph.config.system.build.toplevel
        ) >>"$log" 2>&1; then
          git -C "$clone" diff >"$state/bump-$version.diff"
          bump_failed "$version" "the systems don't build" "$log" "The pin change is in $state/bump-$version.diff (applied, unpushed, in the clone $clone)."
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
        rm -f "$state/bump-tries-$version"
        record "pushed z3code: $version"
        systemctl start --no-block nixos-ci.service 2>/dev/null || true
      }

      status() {
        local want
        echo "host        $host"
        echo "profile     $(running)"
        echo "running     $(server_version)"
        want=$(origin_version 2>/dev/null || true)
        echo "origin/main $want"
        if paused; then sed 's/^/paused      /' "$state/paused"; else echo "paused      no"; fi
        [ -e "$state/paused-bump" ] && sed 's/^/bump paused /' "$state/paused-bump"
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
        after-switch) after_switch ;;
        pause)
          pause "''${2:-paused by hand}"
          ;;
        resume)
          # Applying restarts the T3 server (or the desktop app), which would cut off a thread that ran this; the
          # apply unit instead waits for its gates, as usual.
          rm -f "$state/paused" "$state/paused-bump" "$state"/bump-tries-*
          record "resumed"
          if [ "$bump_host" = true ]; then systemctl --user start --no-block z3-autoupdate-bump.service; fi
          systemctl --user start --no-block z3-autoupdate-apply.service
          say "apply runs once its gates pass (z3-autoupdate status); z3-autoupdate apply --force applies now"
          ;;
        log)
          shift
          journalctl --user --no-pager --lines 200 -u z3-autoupdate-apply.service -u z3-autoupdate-bump.service "$@"
          ;;
        *)
          echo "usage: z3-autoupdate [status|apply [--force]|after-switch|bump|pause [reason]|resume|log [-f]]" >&2
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
    restartWindow = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "4-6";
      description = ''
        Hours "FROM-TO" in which a restart that has waited over a day goes ahead although agent runs are in
        progress (they are cut off), for a host whose agents never stop for long.
      '';
    };
    desktopApp = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Relaunch the Z3 Code desktop app after a switch if it was running.";
    };
  };

  config = {
    environment.systemPackages = [cli];
    # Restart Z3 Code after nixos-deploy switches, and check it.
    nixos-deploy.postSwitch = ["z3-autoupdate after-switch"];

    systemd.user.services = {
      z3-autoupdate-apply = oneshot "Restart Z3 Code onto the profile's version, when idle" "apply";
      z3-autoupdate-bump = lib.mkIf cfg.bump (oneshot "Pin the newest Z3 Code release on main" "bump");
    };

    systemd.user.timers = {
      z3-autoupdate-apply = {
        unitConfig.ConditionUser = user;
        wantedBy = ["timers.target"];
        timerConfig = {
          OnBootSec = "10min";
          OnUnitActiveSec = "1min";
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
