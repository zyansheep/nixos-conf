# sguru-backup: nightly, encrypted, off-host backups of Vaultwarden (the password manager) and headscale (the
# tailnet's control server) from the VPS sguru, pulled to functor.
#
# - Pull, not push: the tailnet policy lets functor open ssh to sguru, but not sguru to functor. A user timer
#   (zyansheep) runs `sguru-backup run`, which ssh'es to root@sguru with zyansheep's ~/.ssh key and runs
#   ./sguru-backup-remote.sh there. That makes consistent copies of both live SQLite databases with SQLite's online
#   backup API (no service is stopped), adds the files a restore needs, and streams them back as a tar:
#     vaultwarden/      db.sqlite3, rsa_key.pem, and attachments/, sends/, config.json when they exist (no icon cache)
#     headscale/data/   db.sqlite (also holds the tailnet policy: database mode), noise_private.key, any other key
#     headscale/etc/    /opt/services/headscale (config.yaml, policy.hujson: the source the policy was set from)
#     headscale/policy.hujson  `headscale policy get`, for reading; the database copy is the one to restore
#     services/docker-compose.yml, MANIFEST (time and the exact image digests, to restore onto the same versions)
#   The tar lands in $XDG_RUNTIME_DIR/sguru-backup/snapshot (tmpfs, removed afterwards), so plaintext never hits
#   functor's disk. sguru gets no new files: its copies are made in /dev/shm and removed on exit.
# - Storage: a restic repository in ~/.local/share/sguru-backup/restic (encrypted, deduplicated), password in
#   ~/.local/share/sguru-backup/restic-password (mode 600, made with the repository on the first run). Keep a copy
#   of that password OFF this machine and outside Vaultwarden (paper, another password store): a backup of the
#   password manager that only the password manager can unlock is no backup.
# - Each run: pull, check both databases, `restic backup`, `forget --prune` (14 daily, 8 weekly, 12 monthly),
#   restore the new snapshot into a temp dir and check it again (PRAGMA integrity_check, row counts), `restic check`.
# - Alerts go to ntfy topic sguru-alerts through sguru's /usr/local/sbin/sguru-notify (over the same ssh): a failed
#   run (OnFailure), and from the daily check timer when the newest snapshot is older than 2 days. If sguru itself
#   is unreachable the alert can't be sent either; it goes to the journal and a desktop notification instead.
#
#   sguru-backup status            snapshots, newest age, timers
#   sguru-backup run               back up now (what the timer runs)
#   sguru-backup verify [SNAP]     restore a snapshot (default latest) into a temp dir and check the databases
#   sguru-backup check             alert if the newest snapshot is older than 2 days
#   sguru-backup restic ARGS...    restic with this repository and password, e.g. `sguru-backup restic snapshots`
#   sguru-backup log [-f]          the units' journal
#
# Restore (onto sguru or a new VPS with the same compose stack):
#   1. On functor:  sguru-backup restic restore latest:/run/user/1000/sguru-backup/snapshot --target /tmp/r
#      (or SNAPSHOT_ID instead of latest; `sguru-backup restic snapshots` lists them). Read /tmp/r/MANIFEST and pin
#      the vaultwarden and headscale images to those digests: a newer image migrates the database forward, and
#      headscale (0.29+) refuses to downgrade or to skip a minor version.
#   2. Copy it over:  rsync -a /tmp/r/ root@sguru:/root/restore/
#   3. On sguru, in /opt/services:  docker compose stop vaultwarden headscale headplane
#      Vaultwarden: in /var/lib/docker/volumes/services_vaultwarden_data/_data remove db.sqlite3-wal and
#        db.sqlite3-shm, then copy in db.sqlite3, rsa_key*, attachments/, sends/, config.json from
#        /root/restore/vaultwarden.
#      headscale: in /var/lib/docker/volumes/services_headscale_data/_data remove db.sqlite-wal and db.sqlite-shm,
#        then copy in /root/restore/headscale/data/*; compare /root/restore/headscale/etc/config.yaml with
#        /opt/services/headscale/config.yaml (a new VPS: copy it in).
#      docker compose up -d vaultwarden headscale headplane, then `docker compose logs` both and log in to
#      https://vault.zyancraft.net; `docker exec services-headscale-1 headscale nodes list`. Delete /root/restore.
#   Without functor: the repository is plain restic. Copy ~/.local/share/sguru-backup/restic anywhere and run
#   `RESTIC_REPOSITORY=<dir> restic restore latest --target /tmp/r` with the password.
_: {
  lib,
  pkgs,
  ...
}: let
  user = "zyansheep";

  cli = pkgs.writeShellApplication {
    name = "sguru-backup";
    runtimeInputs = with pkgs; [
      coreutils
      gnutar
      jq
      libnotify
      openssh
      restic
      sqlite
      systemd
      util-linux
    ];
    text = ''
      host=root@sguru
      dir=''${SGURU_BACKUP_DIR:-$HOME/.local/share/sguru-backup}
      runtime=''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/sguru-backup
      stage=$runtime/snapshot
      max_age_hours=48
      remote_script=${./sguru-backup-remote.sh}
      ssh_opts=(-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

      export RESTIC_REPOSITORY=$dir/restic
      export RESTIC_PASSWORD_FILE=$dir/restic-password
      export RESTIC_CACHE_DIR=''${XDG_CACHE_HOME:-$HOME/.cache}/restic
      export RESTIC_PROGRESS_FPS=0.016

      say() { echo "sguru-backup: $*" >&2; }

      # ntfy via sguru's helper; falls back to the journal and a desktop notification.
      notify() {
        local prio=$1 title=$2 tags=$3 body=$4
        # shellcheck disable=SC2029 # expanded here on purpose, quoted for the remote shell with %q
        if ! printf '%s\n' "$body" | ssh "''${ssh_opts[@]}" "$host" \
          "/usr/local/sbin/sguru-notify $(printf '%q ' "$prio" "$title" "$tags")"; then
          logger -t sguru-backup "could not send the alert through sguru: $title: $body" || true
          notify-send --urgency critical --app-name "sguru backup" "$title" "$body" 2>/dev/null || true
        fi
      }

      init() {
        umask 077
        mkdir -p "$dir" "$runtime"
        chmod 700 "$dir" "$runtime"
        if [ -e "$RESTIC_REPOSITORY/config" ]; then
          [ -s "$RESTIC_PASSWORD_FILE" ] || { say "$RESTIC_PASSWORD_FILE is missing; restore it from your offline copy"; exit 1; }
          return
        fi
        if [ ! -s "$RESTIC_PASSWORD_FILE" ]; then
          head -c 32 /dev/urandom | base64 -w0 >"$RESTIC_PASSWORD_FILE.tmp"
          mv "$RESTIC_PASSWORD_FILE.tmp" "$RESTIC_PASSWORD_FILE"
          say "new repository password in $RESTIC_PASSWORD_FILE: keep a copy off this machine"
        fi
        restic init
      }

      # Integrity and a row count of both databases under $1.
      check_dbs() {
        local root=$1 db r
        for db in vaultwarden/db.sqlite3 headscale/data/db.sqlite; do
          [ -s "$root/$db" ] || { say "$db missing from $root"; return 1; }
          r=$(sqlite3 -readonly "$root/$db" 'PRAGMA integrity_check;')
          [ "$r" = ok ] || { say "$db: integrity_check: $r"; return 1; }
        done
        echo "vaultwarden: integrity ok, $(sqlite3 -readonly "$root/vaultwarden/db.sqlite3" \
          "SELECT (SELECT count(*) FROM users) || ' users, ' || (SELECT count(*) FROM ciphers) || ' items';")"
        echo "headscale:   integrity ok, $(sqlite3 -readonly "$root/headscale/data/db.sqlite" \
          "SELECT (SELECT count(*) FROM users) || ' users, ' || (SELECT count(*) FROM nodes) || ' nodes, ' || (SELECT count(*) FROM policies) || ' policies';")"
        [ -s "$root/vaultwarden/rsa_key.pem" ] || { say "vaultwarden/rsa_key.pem missing"; return 1; }
        [ -s "$root/headscale/data/noise_private.key" ] || { say "headscale noise_private.key missing"; return 1; }
      }

      pull() {
        local attempt
        for attempt in 1 2 3; do
          rm -rf "$stage"
          mkdir -p "$stage"
          if ssh "''${ssh_opts[@]}" "$host" 'bash -s' <"$remote_script" | tar -C "$stage" -xf -; then
            return 0
          fi
          say "pull attempt $attempt failed"
          [ "$attempt" = 3 ] || sleep 60
        done
        return 1
      }

      verify() {
        local snap=''${1:-latest} tmp rc=0
        tmp=$(mktemp -d "$runtime/verify.XXXXXX")
        { restic restore --quiet "$snap:$stage" --target "$tmp" && check_dbs "$tmp"; } || rc=$?
        rm -rf "$tmp"
        return "$rc"
      }

      run() {
        init
        exec 9>"$runtime/lock"
        flock -n 9 || { say "another run holds $runtime/lock"; exit 1; }
        trap 'rm -rf "$stage"' EXIT
        pull
        check_dbs "$stage" >/dev/null
        restic backup --host sguru --tag sguru "$stage"
        restic forget --host sguru --prune --keep-daily 14 --keep-weekly 8 --keep-monthly 12
        verify latest
        restic check
        say "done; $(restic stats --json latest | jq -r '"latest restores to \(.total_size) bytes in \(.total_file_count) files"')"
      }

      newest_age_hours() {
        local t
        t=$(restic snapshots --host sguru --latest 1 --json | jq -r 'max_by(.time) | .time // empty')
        [ -n "$t" ] || { echo none; return; }
        echo $(( ($(date +%s) - $(date -d "$t" +%s)) / 3600 ))
      }

      check() {
        local age
        if ! age=$(newest_age_hours); then
          notify high "functor: sguru backup repository unreadable" warning \
            "restic snapshots failed in $RESTIC_REPOSITORY on functor. sguru-backup status there."
          exit 1
        fi
        if [ "$age" = none ] || [ "$age" -ge "$max_age_hours" ]; then
          notify high "functor: sguru backup is stale" warning \
            "Newest Vaultwarden/headscale snapshot on functor: ''${age} hours old (limit $max_age_hours). sguru-backup status / sguru-backup log there."
          exit 1
        fi
        say "newest snapshot is $age hours old"
      }

      alert_failed() {
        notify high "functor: sguru backup failed" "warning,floppy_disk" \
          "$(journalctl --user --no-pager -o cat -n 20 -u sguru-backup.service 2>/dev/null || echo 'no journal')"
      }

      status() {
        restic snapshots --host sguru --compact || true
        echo "newest: $(newest_age_hours || echo '?') hours old"
        echo "repository: $RESTIC_REPOSITORY ($(du -sh "$RESTIC_REPOSITORY" 2>/dev/null | cut -f1))"
        systemctl --user list-timers --no-pager --all sguru-backup.timer sguru-backup-check.timer 2>/dev/null || true
      }

      case ''${1:-status} in
        run) run ;;
        verify)
          mkdir -p "$runtime"
          verify "''${2:-latest}"
          ;;
        check) check ;;
        alert-failed) alert_failed ;;
        status) status ;;
        restic)
          shift
          restic "$@"
          ;;
        log)
          shift
          journalctl --user --no-pager -n 200 -u sguru-backup.service -u sguru-backup-check.service -u sguru-backup-failed.service "$@"
          ;;
        *)
          echo "usage: sguru-backup [status|run|verify [SNAPSHOT]|check|restic ARGS...|log [-f]]" >&2
          exit 2
          ;;
      esac
    '';
  };

  oneshot = description: args: {
    inherit description;
    unitConfig.ConditionUser = user;
    # A switch must not restart (and so kill) a run in progress.
    restartIfChanged = false;
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${lib.getExe cli} ${args}";
      Nice = 10;
      IOSchedulingClass = "idle";
    };
  };

  timer = calendar: {
    unitConfig.ConditionUser = user;
    wantedBy = ["timers.target"];
    timerConfig = {
      OnCalendar = calendar;
      Persistent = true;
      RandomizedDelaySec = "15min";
    };
  };
in {
  environment.systemPackages = [cli];

  systemd.user.services = {
    sguru-backup =
      lib.recursiveUpdate (oneshot "Pull Vaultwarden and headscale backups from sguru into restic" "run")
      {
        onFailure = ["sguru-backup-failed.service"];
        serviceConfig.TimeoutStartSec = "1h";
      };
    sguru-backup-failed = oneshot "Alert ntfy that the sguru backup failed" "alert-failed";
    sguru-backup-check = oneshot "Alert ntfy when the newest sguru backup is over 2 days old" "check";
  };

  systemd.user.timers = {
    # 03:30 Pacific: clear of sguru's updates (~06:00 UTC), reboot window (13:00 UTC) and image pulls (Sun 17:00 UTC).
    sguru-backup = timer "*-*-* 03:30:00";
    sguru-backup-check = timer "*-*-* 12:00:00";
  };
}
