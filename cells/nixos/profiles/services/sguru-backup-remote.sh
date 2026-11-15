#!/bin/bash
# Runs as root on sguru, sent over ssh by `sguru-backup` on functor (sguru-backup.nix). Writes a tar of what a
# restore of Vaultwarden and headscale needs to stdout; everything else goes to stderr. Changes nothing on sguru:
# the copies are made in a tmpfs directory that is removed on exit, and the services keep running.
#
# Everything is inside main(), which runs with stdin from /dev/null: bash reads this script from ssh's stdin, so it
# must have parsed all of it before any command could read from stdin.
set -euo pipefail
umask 077

vw=/var/lib/docker/volumes/services_vaultwarden_data/_data
hs=/var/lib/docker/volumes/services_headscale_data/_data
srv=/opt/services

# Consistent copies of the live WAL-mode databases with SQLite's online backup API (sguru has no sqlite3 binary;
# python3's module is enough). Host and container share the kernel, so SQLite's locks work across the bind mount.
copy_dbs() {
  python3 - "$@" <<'PY'
import sqlite3, sys
args = sys.argv[1:]
for src, dst in zip(args[::2], args[1::2]):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    d = sqlite3.connect(dst)
    s.backup(d)
    s.close()
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    d.execute("PRAGMA journal_mode=DELETE")
    d.close()
    if ok != "ok":
        sys.exit(f"{src}: integrity_check of the copy: {ok}")
    print(f"copied {src}", file=sys.stderr)
PY
}

main() {
  local work f
  work=$(mktemp -d /dev/shm/sguru-backup.XXXXXX)
  # shellcheck disable=SC2064 # expand now: $work is local
  trap "rm -rf '$work'" EXIT
  mkdir -p "$work/vaultwarden" "$work/headscale/data" "$work/headscale/etc" "$work/services"

  copy_dbs "$vw/db.sqlite3" "$work/vaultwarden/db.sqlite3" "$hs/db.sqlite" "$work/headscale/data/db.sqlite"

  # Vaultwarden: the key that signs logins, attachments, sends, the admin config. Not icon_cache or tmp.
  shopt -s nullglob
  for f in "$vw"/rsa_key* "$vw"/config.json "$vw"/attachments "$vw"/sends; do
    [ -e "$f" ] || continue
    cp -a "$f" "$work/vaultwarden/"
  done

  # headscale: every file in its volume but the database (the noise key, a derp key if one appears, a policy file
  # if the policy ever moves out of the database), its config directory, and the policy as the server sees it.
  for f in "$hs"/* "$hs"/.[!.]*; do
    case ${f##*/} in db.sqlite | db.sqlite-*) continue ;; esac
    cp -a "$f" "$work/headscale/data/"
  done
  cp -a "$srv/headscale/." "$work/headscale/etc/"
  docker exec services-headscale-1 headscale policy get >"$work/headscale/policy.hujson" 2>/dev/null ||
    echo "headscale policy get failed (the policy is still in the database copy)" >&2

  cp -a "$srv/docker-compose.yml" "$work/services/"
  {
    echo "taken    $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
    # The registry digest (what an image: line can pin, name@sha256:...), else the local image id.
    for f in services-vaultwarden-1 services-headscale-1; do
      echo "image    $f $(docker inspect --format '{{.Config.Image}}' "$f") $(docker image inspect \
        --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' \
        "$(docker inspect --format '{{.Image}}' "$f")")"
    done
  } >"$work/MANIFEST"

  tar -C "$work" --numeric-owner -cf - .
}

main </dev/null
