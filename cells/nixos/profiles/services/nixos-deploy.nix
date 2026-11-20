# nixos-deploy: keep each host on the newest commit of main that nixos-ci built for it, the way dither-chat's
# deployer does: only green commits, checked after the switch, rolled back if a check fails.
#
# Every 15 minutes (a user service of zyansheep's):
#   1. Where this host stands: the commit it runs (`nixos-version --configuration-revision`), or, for a local build
#      (`nrb` from a tree with uncommitted changes, "<rev>-dirty"), the commit `rebuild` recorded for the tree it
#      built (core.rebuild; /var/lib/nixos-deploy/local-builds). Its base is where that meets main.
#   2. The candidates are the commits on main after the base, newest first; it takes the first that nixos-ci built
#      for this host (`nixos-ci results` on functor) and that hasn't failed here before.
#      Every commit between the base and the candidate must carry a good SSH signature from a key in
#      cells/home/profiles/_files/git-allowed-signers, as that file was in the RUNNING system (baked in at build
#      time, so a pushed commit can't vouch for itself). A commit that isn't signed blocks it and every candidate
#      after it, and says so once; `nrb` from a tree you checked is the way past one.
#   3. A host that isn't the CI host (isomorph) is the second wave: it only takes a candidate that the CI host
#      (functor, the canary) already runs, or has moved past, and never one the CI host marked bad.
#   4. A local build is never switched away from while main lacks its changes: the candidate must have, for every
#      file the local build changed, the same contents (nixos-deploy.liveFiles excepted). Otherwise it waits and
#      says so once; `nixos-deploy drop-local` lets it go anyway, and `nrb` from a tree with main merged in moves on.
#   5. It waits only for what a switch itself disturbs: no builds running, on AC power. Agent runs and someone at
#      the machine don't hold it: a switch restarts no T3 server (their units have restartIfChanged = false), and
#      z3-autoupdate restarts Z3 Code on its own gates (`nixos-deploy busy --restart`).
#   6. It switches to nixos-ci's store path, through update-manager-switch@ (fetched from functor's cache with
#      signatures checked). ~/nixos-conf is fast-forwarded too when it can be, but needn't be.
#   7. Checks: no unit failed that hadn't before, every unit in nixos-deploy.checkUnits that was active still is,
#      and each nixos-deploy.postSwitch command prints nothing and succeeds.
#   8. A switch that fails without switching anything (a fetch, the cache) is tried again on the next 2 runs. A failed
#      check switches back to the previous system, runs the postSwitch commands again, marks the commit bad (never
#      tried again on this host), pauses, and starts a T3 thread in nixos-conf to find out why.
#
#   nixos-deploy status           what runs, what would deploy, gates, pause, history
#   nixos-deploy plan             what a run would deploy now, without the gates or switching
#   nixos-deploy apply [--force]  run now (--force skips the gates and the canary)
#   nixos-deploy pause [reason] / resume
#   nixos-deploy hold / unhold    keep this host where it is until unhold (a pause that starts no thread)
#   nixos-deploy drop-local       let the next deploy replace this host's local build although main lacks its changes
#   nixos-deploy busy [--restart] why a switch (or, with --restart, a restart of Z3 Code) should wait, or nothing
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
  # Read from this build, never from the commit being checked.
  allowedSigners = ../../../home/profiles/_files/git-allowed-signers;

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
      local_builds=/var/lib/nixos-deploy/local-builds
      host=${config.networking.hostName}
      ci_host=${cfg.ciHost}
      on_ci_host=${lib.boolToString onCiHost}
      check_units=(${lib.escapeShellArgs cfg.checkUnits})
      post_switch=(${lib.escapeShellArgs cfg.postSwitch})
      live_files=(${lib.escapeShellArgs cfg.liveFiles})
      alert_command=${lib.escapeShellArg (toString cfg.alertCommand)}
      thread_args=(--provider ${lib.escapeShellArg cfg.thread.provider} --model ${lib.escapeShellArg cfg.thread.model})
      t3_db=$HOME/.t3/userdata/statev2.sqlite
      clone=$state/main.git
      allowed_signers=${allowedSigners}
      mkdir -p "$state/bad" "$state/tries" "$state/dropped" "$runtime"

      say() { echo "nixos-deploy: $*" >&2; }
      record() { printf '%s %s\n' "$(date -Is)" "$*" >>"$state/history"; say "$*"; }
      # $1 title, $2 body, $3 urgency (low, normal, critical): a notification here, and nixos-deploy.alertCommand,
      # which reaches the user wherever they are.
      alert() {
        notify-send --app-name "nixos-deploy" --urgency "''${3:-normal}" "$1" "''${2:-}" 2>/dev/null || true
        if [ -n "$alert_command" ]; then
          bash -c "$alert_command" alert "$1" "''${2:-}" "''${3:-normal}" >>"$state/alert.log" 2>&1 || true
        fi
      }
      wait_for() {
        echo "$(date -Is) $1" >"$state/waiting"
        say "$1"
      }
      # Says $2 once per key $1, in the history and as an alert.
      tell_once() {
        [ -e "$state/told-$1" ] && return 0
        touch "$state/told-$1"
        record "$2"
        alert "Update waiting on $host" "$2" low
      }

      paused() { [ -e "$state/paused" ]; }
      pause() {
        { echo "since   $(date -Is)"; echo "reason  $1"; } >"$state/paused"
        record "paused: $1"
      }

      # --- gates ------------------------------------------------------------------------------------------------
      graphical_session() {
        local s
        while read -r s _; do
          [ "$(loginctl show-session "$s" -p Name --value 2>/dev/null)" = "$USER" ] || continue
          [ "$(loginctl show-session "$s" -p Type --value 2>/dev/null)" = wayland ] || continue
          [ "$(loginctl show-session "$s" -p State --value 2>/dev/null)" = active ] && return 0
        done < <(loginctl list-sessions --no-legend 2>/dev/null)
        return 1
      }

      t3_runs() {
        [ -r "$t3_db" ] || { echo 0; return 0; }
        # Runs left "running" by a crash would block forever, so only recent ones count.
        sqlite3 -readonly "$t3_db" "select count(*) from orchestration_v2_projection_runs
          where status in ('queued', 'preparing', 'starting', 'running')
            and julianday(requested_at) > julianday('now', '-12 hours')" 2>/dev/null || echo 0
      }

      # Prints why a switch should wait, or nothing.
      switch_busy() {
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

      # Prints why restarting the T3 server or the desktop app should wait, or nothing: a switch is being made or
      # checked (its check would see the units restarting), an agent run would be cut off, or (with a graphical
      # session) someone is using the machine.
      restart_busy() {
        local runs
        if systemctl --user is-active --quiet nixos-deploy.service ||
          [ -n "$(systemctl list-units --no-legend --plain --state=activating,active 'update-manager-switch@*' 'update-manager-boot@*' 2>/dev/null)" ]; then
          echo "a switch is under way"
          return
        fi
        runs=$(t3_runs)
        [ "''${runs:-0}" -gt 0 ] && { echo "$runs T3 agent run(s) in progress"; return; }
        if graphical_session && [ ! -e "$runtime/idle" ] && ! pgrep -x swaylock >/dev/null; then
          echo "someone is at the machine (input in the last 15 minutes, screen unlocked)"
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

      # Prints the CI host's running commit (without -dirty) on the first line, then the commits it marked bad.
      canary_state() {
        ssh -o BatchMode=yes -o ConnectTimeout=5 "$ci_host" \
          'r=$(nixos-version --configuration-revision 2>/dev/null); echo "''${r%-dirty}"; ls "''${XDG_STATE_HOME:-$HOME/.local/state}/nixos-deploy/bad" 2>/dev/null; true'
      }

      # Prints the first commit in $1..$2 (oldest first) without a good signature from an allowed signer, or nothing.
      # Fails closed: if the range can't be listed, $2 itself counts as unsigned.
      unsigned_commit() {
        local c list
        list=$(git -C "$clone" rev-list --reverse "$1..$2") || { echo "$2"; return 0; }
        while read -r c; do
          [ -n "$c" ] || continue
          git -C "$clone" -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$allowed_signers" \
            verify-commit "$c" >/dev/null 2>&1 || { echo "$c"; return 0; }
        done <<<"$list"
        return 0
      }

      # Prints a commit of ~/nixos-conf's tracked files as they are on disk, parented on HEAD, for a local build
      # that `rebuild` didn't record (one from before it recorded them); nothing if the tree has no changes.
      snapshot_tree() {
        local index tree commit
        index=$(mktemp)
        cp "$(git -C "$conf" rev-parse --path-format=absolute --git-path index)" "$index" || { rm -f "$index"; return 1; }
        GIT_INDEX_FILE=$index git -C "$conf" add -u
        # Files programs edit live stay as HEAD has them, so editing them doesn't make another local build.
        [ ''${#live_files[@]} -eq 0 ] || GIT_INDEX_FILE=$index git -C "$conf" reset -q HEAD -- "''${live_files[@]}"
        tree=$(GIT_INDEX_FILE=$index git -C "$conf" write-tree)
        rm -f "$index"
        [ "$tree" != "$(git -C "$conf" rev-parse "HEAD^{tree}")" ] || return 0
        # Dated like HEAD, so the same tree gives the same commit on every run.
        commit=$(GIT_AUTHOR_DATE=$(git -C "$conf" log -1 --format=%cI HEAD) GIT_COMMITTER_DATE=$(git -C "$conf" log -1 --format=%cI HEAD) \
          git -C "$conf" commit-tree "$tree" -p HEAD -m "local build on $host (snapshot by nixos-deploy)")
        git -C "$conf" update-ref "refs/nixos-deploy/local/$commit" "$commit"
        echo "$commit"
      }

      # Prints the files whose changes from $1 to local build $2 candidate $3 lacks (nixos-deploy.liveFiles left
      # out), or nothing: the candidate holds the changes if the patch $1..$2 applies to it in reverse. Files main
      # changed too (flake.lock) count as held when the local build's hunks are there, whatever else main did.
      local_changes_missing() {
        local f changed=() patch index names missing=""
        # A list that can't be made (a blob fetch offline) holds the deploy rather than letting it through.
        names=$(git -C "$clone" diff --no-renames --name-only "$1" "$2") || { echo "(could not list the local build's changes)"; return 0; }
        while read -r f; do
          [ -n "$f" ] || continue
          printf '%s\n' "''${live_files[@]}" | grep -qxF -- "$f" && continue
          changed+=("$f")
        done <<<"$names"
        [ ''${#changed[@]} -gt 0 ] || return 0
        patch=$(mktemp)
        index=$(mktemp -u)
        git -C "$clone" diff --no-renames --binary "$1" "$2" -- "''${changed[@]}" >"$patch"
        GIT_INDEX_FILE=$index git -C "$clone" read-tree "$3"
        if ! GIT_INDEX_FILE=$index git -C "$clone" apply --cached --reverse --check "$patch" 2>/dev/null; then
          # Name the files that differ, for the message.
          for f in "''${changed[@]}"; do
            git -C "$clone" diff --no-renames --binary "$1" "$2" -- "$f" >"$patch"
            GIT_INDEX_FILE=$index git -C "$clone" apply --cached --reverse --check "$patch" 2>/dev/null || missing+="$f"$'\n'
          done
          [ -n "$missing" ] || missing="(the changes together)"
        fi
        rm -f "$patch" "$index"
        printf '%s' "$missing"
      }

      # Sets running (what `nixos-version` says), local (the local build's commit, or empty), base (where this host
      # meets main), candidate and candidate_path (the commit to deploy and nixos-ci's system for it), and blocked
      # (why a candidate can't be used yet, or empty); or sets why (there's nothing to deploy) and returns 1.
      # Not in $(...): the caller needs the variables.
      plan() {
        local rev revs results path tip tip_result unsigned recorded at missing sigbase head
        local unsigned_seen="" canary_rev="" canary_bad="" canary_skipped=""
        why=
        blocked=
        lbuild=
        running=$(nixos-version --configuration-revision 2>/dev/null || true)
        at=''${running%-dirty}
        if ! [[ $at =~ ^[0-9a-f]{40}$ ]]; then
          why="the running system doesn't record its commit ('$running'): switch once with nrb"
          return 1
        fi
        [ -d "$clone" ] || git clone --quiet --bare --filter=blob:none "$url" "$clone"
        git -C "$clone" fetch --quiet origin +refs/heads/main:refs/heads/main || {
          why="could not fetch main"
          return 1
        }
        if [[ $running == *-dirty ]]; then
          recorded="$local_builds/$(basename "$(readlink -f /run/current-system)")"
          if [ -r "$recorded" ]; then
            lbuild=$(cat "$recorded")
          else
            # Built before `rebuild` recorded local builds: the tree as it is now is the best guess, if it still
            # holds the commit that was built.
            head=$(git -C "$conf" rev-parse HEAD 2>/dev/null || true)
            if [ -n "$head" ] && git -C "$conf" merge-base --is-ancestor "$at" "$head" 2>/dev/null; then
              if ! lbuild=$(snapshot_tree); then
                why="could not read $conf to tell what this host's local build holds"
                return 1
              fi
              # A clean tree: what was built is now committed, so HEAD stands for it.
              [ -n "$lbuild" ] || lbuild=$head
            fi
          fi
          if [ -z "$lbuild" ]; then
            why="this host runs a local build of ''${at:0:12} whose tree is unknown (no record, and $conf moved on): nrb once to record it"
            return 1
          fi
        elif ! git -C "$clone" merge-base --is-ancestor "$at" main 2>/dev/null; then
          # A commit off main (nrb from a branch): it counts as a local build.
          lbuild=$at
        fi
        # Signatures are checked from the newest commit of main this host is known to run.
        sigbase=$at
        if [ -n "$lbuild" ]; then
          git -C "$clone" fetch --quiet "$conf" "+refs/nixos-deploy/*:refs/conf/nixos-deploy/*" "+refs/heads/*:refs/conf/heads/*" 2>/dev/null || true
          if ! git -C "$clone" cat-file -e "$lbuild^{commit}" 2>/dev/null; then
            why="this host runs ''${lbuild:0:12}, which neither main nor $conf has: nrb from main to rejoin"
            return 1
          fi
          base=$(git -C "$clone" merge-base "$lbuild" main) || {
            why="this host's build ''${lbuild:0:12} shares no history with main"
            return 1
          }
          git -C "$clone" merge-base --is-ancestor "$at" main 2>/dev/null || sigbase=$(git -C "$clone" merge-base "$at" main 2>/dev/null || echo "$base")
          # A local build that is on main after all (committed, merged) is no local build.
          if git -C "$clone" merge-base --is-ancestor "$lbuild" main; then base=$lbuild; lbuild=; fi
        else
          base=$at
        fi

        mapfile -t revs < <(git -C "$clone" rev-list --first-parent --max-count 50 "$base..main")
        if [ ''${#revs[@]} -eq 0 ]; then
          if [ -n "$lbuild" ]; then why="runs a local build of ''${base:0:12}, the tip of main"; else why="up to date with main (''${base:0:12})"; fi
          return 1
        fi
        tip=''${revs[0]}
        results=$(ci_results "''${revs[@]}") || { why="could not ask $ci_host for nixos-ci's results"; return 1; }
        if [ "$on_ci_host" != true ]; then
          local cs
          cs=$(canary_state) || { why="could not reach $ci_host to check what it runs (the canary)"; return 1; }
          canary_rev=$(head -n 1 <<<"$cs")
          canary_bad=$(tail -n +2 <<<"$cs")
        fi
        candidate=
        candidate_path=
        for rev in "''${revs[@]}"; do
          [ -e "$state/bad/$rev" ] && continue
          path=$(awk -v rev="$rev" -v host="$host" '
            $1 == "rev" { cur = $2; next }
            cur == rev && $1 == host && $2 == "ok" { print $3 }' <<<"$results")
          [ -n "$path" ] || continue
          if [ "$on_ci_host" != true ] && [ "''${CANARY:-1}" = 1 ]; then
            grep -qxF "$rev" <<<"$canary_bad" && continue
            if ! git -C "$clone" merge-base --is-ancestor "$rev" "$canary_rev" 2>/dev/null; then
              canary_skipped=$rev
              continue
            fi
          fi
          unsigned=$(unsigned_commit "$sigbase" "$rev")
          if [ -n "$unsigned" ]; then
            # Older candidates may still be clean of it; but say so, once per commit.
            tell_once "unsigned-$unsigned" "commit ''${unsigned:0:12} on main isn't signed by a trusted key: not deploying it or anything after it"
            unsigned_seen=$unsigned
            continue
          fi
          candidate=$rev
          candidate_path=$path
          break
        done
        if [ -z "$candidate" ] && [ -n "$unsigned_seen" ]; then
          why="commit ''${unsigned_seen:0:12} on main isn't signed by a key in git-allowed-signers; nothing after it deploys (nrb from a checked tree to move past it)"
          return 1
        fi
        if [ -z "$candidate" ]; then
          tip_result=$(awk -v rev="$tip" -v host="$host" '$1 == "rev" { cur = $2; next } cur == rev && $1 == host { print $2 }' <<<"$results")
          if [ -n "$canary_skipped" ]; then
            why="waiting for $ci_host (the canary) to run ''${canary_skipped:0:12} first (it runs ''${canary_rev:0:12})"
          elif [ "$tip_result" = FAILED ]; then
            why="main (''${tip:0:12}) doesn't build for $host, and no commit between it and ''${base:0:12} did"
          else
            why="nixos-ci hasn't built a newer commit for $host yet (main is ''${tip:0:12})"
          fi
          return 1
        fi
        if [ -n "$lbuild" ] && [ ! -e "$state/dropped/$(basename "$(readlink -f /run/current-system)")" ]; then
          missing=$(local_changes_missing "$base" "$lbuild" "$candidate")
          if [ -n "$missing" ]; then
            blocked="$host runs a local build (''${lbuild:0:12}) whose changes to $(echo "$missing" | tr '\n' ' ')aren't on main as built: commit them to main, or nixos-deploy drop-local to let them go"
          fi
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
        if out=$(printf '%s\n' "$body" | t3 thread start "$conf" - --title "$title" "''${thread_args[@]}" --json 2>>"$state/thread.err"); then
          link=$(jq -r '.link // .threadId // empty' <<<"$out" 2>/dev/null || true)
          echo "thread  ''${link:-$out}" >>"$state/paused"
          record "debug thread: ''${link:-$out}"
          alert "$title" "Paused. A T3 thread on $host is looking into it: ''${link:-see nixos-deploy status}" critical
        else
          record "could not start the debug thread (see $state/thread.err)"
          alert "$title" "Paused. Could not start a T3 thread; see nixos-deploy status on $host." critical
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
      then run \`nixos-deploy resume\` on $host: it deploys once nixos-ci has built the fix. Don't run
      \`nixos-deploy apply --force\` yourself.

      If it isn't clear, or the fix is risky, change nothing: leave deploys paused, and end with what broke, the
      evidence, and the options, for the user to review.
      EOF
      }

      # --- apply --------------------------------------------------------------------------------------------------
      apply() {
        local force=$1 reason prev log failed_before active_before problem="" running lbuild base candidate candidate_path
        local why blocked tries
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run is in progress"; return 0; }
        if paused; then say "paused ($(sed -n 's/^reason  //p' "$state/paused"))"; return 0; fi

        if [ "$force" = 1 ]; then export CANARY=0; fi
        if ! plan; then
          wait_for "$why"
          return 0
        fi
        if [ -n "$blocked" ]; then
          wait_for "$blocked"
          tell_once "local-$(basename "$(readlink -f /run/current-system)")" "$blocked"
          return 0
        fi
        if [ "$force" != 1 ]; then
          reason=$(switch_busy || true)
          if [ -n "$reason" ]; then
            wait_for "waiting to deploy ''${candidate:0:12}: $reason"
            return 0
          fi
        fi
        rm -f "''${state:?}/waiting"

        log=$state/deploy-''${candidate:0:12}.log
        : >>"$log"
        prev=$(readlink -f /run/current-system)
        failed_before=$(failed_units)
        active_before=$(active_check_units)
        record "deploying ''${candidate:0:12} ($candidate_path, running ''${running:0:12}$([ -n "$lbuild" ] && echo ", a local build ''${lbuild:0:12}"))"
        if ! systemctl start "$(switch_unit "$candidate_path")" >>"$log" 2>&1; then
          problem="the switch to $candidate_path failed ($(switch_unit "$candidate_path"))"
          if [ "$(readlink -f /run/current-system)" = "$prev" ]; then
            # Nothing switched: a fetch or the cache, most likely. Try again on the next runs before calling it bad.
            tries=$(($(cat "$state/tries/$candidate" 2>/dev/null || echo 0) + 1))
            echo "$tries" >"$state/tries/$candidate"
            if [ "$tries" -lt 3 ]; then
              record "$problem; nothing switched, trying again next run ($tries of 3)"
              return 0
            fi
          fi
        fi
        # The tree follows when it can; it doesn't have to.
        if [ "$(git -C "$conf" symbolic-ref --short HEAD 2>/dev/null)" = main ]; then
          git -C "$conf" fetch --quiet origin main >/dev/null 2>&1 &&
            git -C "$conf" merge --ff-only --quiet "$candidate" >/dev/null 2>&1 || true
        fi
        if [ -z "$problem" ]; then
          problem=$(health "$candidate_path" "$failed_before" "$active_before" 2>&1 | tee -a "$log" || true)
        fi
        if [ -z "$problem" ]; then
          rm -f "''${state:?}/tries/$candidate"
          record "deployed ''${candidate:0:12}"
          notify-send --app-name "nixos-deploy" "System updated" "$host runs ''${candidate:0:12} of nixos-conf now" 2>/dev/null || true
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
        local running lbuild base candidate candidate_path why blocked
        echo "host        $host"
        echo "running     $(nixos-version --configuration-revision 2>/dev/null || echo unknown)"
        if plan; then
          [ -n "$lbuild" ] && echo "local build $lbuild (base ''${base:0:12})"
          echo "next        $candidate $candidate_path"
          [ -n "$blocked" ] && echo "blocked     $blocked"
        else
          [ -n "$lbuild" ] && echo "local build $lbuild"
          echo "next        none: $why"
        fi
        if paused; then sed 's/^/paused      /' "$state/paused"; else echo "paused      no"; fi
        [ -e "$state/waiting" ] && echo "waiting     $(cat "$state/waiting")"
        echo "busy now    $(switch_busy || true)"
        echo
        tail -n 8 "$state/history" 2>/dev/null || true
        echo
        systemctl --user list-timers --no-pager --all nixos-deploy.timer 2>/dev/null || true
      }

      case ''${1:-status} in
        status) status ;;
        plan)
          if plan; then
            echo "would deploy $candidate: $candidate_path"
            [ -z "$blocked" ] || echo "but: $blocked"
          else
            echo "$why"
          fi
          ;;
        apply)
          if [ "''${2:-}" = --force ]; then apply 1; else apply 0; fi
          ;;
        busy)
          if [ "''${2:-}" = --restart ]; then restart_busy || true; else switch_busy || true; fi
          ;;
        pause) pause "''${2:-paused by hand}" ;;
        hold) pause "held by hand (nixos-deploy unhold)" ;;
        drop-local)
          plan || true
          if [ -z "''${lbuild:-}" ]; then say "this host runs no local build"; exit 0; fi
          touch "$state/dropped/$(basename "$(readlink -f /run/current-system)")"
          record "the local build ''${lbuild:0:12} may be replaced by main's build"
          systemctl --user start --no-block nixos-deploy.service
          ;;
        resume | unhold)
          rm -f "''${state:?}/paused"
          record "resumed"
          systemctl --user start --no-block nixos-deploy.service
          say "deploys on its next run (nixos-deploy status); nixos-deploy apply --force deploys now"
          ;;
        log)
          shift
          journalctl --user --no-pager --lines 200 -u nixos-deploy.service "$@"
          ;;
        *)
          echo "usage: nixos-deploy [status|plan|apply [--force]|busy [--restart]|pause [reason]|resume|hold|unhold|drop-local|log [-f]]" >&2
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
      description = ''
        The host that runs nixos-ci (reached as `ssh <ciHost>` from the others). It is also the canary: the other
        hosts only deploy a commit it already runs.
      '';
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
    liveFiles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = ''
        Paths in nixos-conf that running programs edit in ~/nixos-conf and read from there (the kaomoji list), so a
        local build's changes to them don't hold a deploy.
      '';
    };
    alertCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        A shell command that tells the user something wherever they are, beyond this host's notifications: run as
        `bash -c CMD alert TITLE BODY URGENCY` (urgency low, normal or critical).
      '';
    };
    thread = {
      provider = lib.mkOption {
        type = lib.types.str;
        default = "claudeAgent";
        description = "T3 provider instance for the threads that look into a failed deploy.";
      };
      model = lib.mkOption {
        type = lib.types.str;
        default = "claude-opus-5-5";
        description = "Model for those threads (`t3 thread start` needs one: the nixos-conf project has no default).";
      };
    };
  };

  config = {
    environment.systemPackages = [cli];
    # rebuild (core.rebuild) records local builds here.
    systemd.tmpfiles.rules = ["d /var/lib/nixos-deploy/local-builds 0755 root root -"];

    systemd.user.services = {
      nixos-deploy = {
        description = "Deploy the newest commit of nixos-conf that nixos-ci built";
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
      # Marks the session idle after 15 minutes without input, for the restart gate (`nixos-deploy busy --restart`).
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
