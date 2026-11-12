# nrb-request REASON [rebuild args...]: ask the user to approve an `nrb` (git add . && sudo rebuild ARGS),
# for agents, whose NoNewPrivileges makes sudo fail (rebuild.nix). Starts nrb-approve.service under the
# user's systemd manager, streams its output, and exits with its status once the user answers:
# 0 switched (or nothing to switch), 77 denied, 75 no answer, 69 no notification daemon, otherwise the
# failed build's or rebuild's status. One request at a time; a second one waits for the first.
unit=${NRB_UNIT:-nrb-approve.service}
dir=${NRB_DIR:-${XDG_RUNTIME_DIR:?}/nrb-approve}

usage() {
  cat <<'EOF'
usage: nrb-request REASON [rebuild args...]

Asks the user (a notification with Approve / Deny) to stage ~/nixos-conf and switch to it: `git add .`, then
`sudo rebuild ARGS`. The notification shows REASON and what changes (nix store diff-closures). Blocks until
the user answers; run it in the background. Exit: 0 switched or nothing to switch, 77 denied, 75 no answer
within 15 minutes, 69 no notification daemon, otherwise the failed build's or rebuild's status.
If this command is interrupted the request stays open: `journalctl --user -u nrb-approve` shows the outcome.
EOF
}
case ${1-} in
  -h | --help)
    usage
    exit 0
    ;;
  "" | -*)
    usage >&2
    exit 64
    ;;
esac

# The rebuild uses ~/nixos-conf: commits in another worktree don't count until they are there.
if top=$(git rev-parse --show-toplevel 2>/dev/null) && [ "$top" != "${NRB_FLAKE_DIR:-$HOME/nixos-conf}" ] &&
  [ "$(git rev-parse --path-format=absolute --git-common-dir)" = "${NRB_FLAKE_DIR:-$HOME/nixos-conf}/.git" ]; then
  echo "nrb-request: note: this rebuilds ${NRB_FLAKE_DIR:-$HOME/nixos-conf}, not this worktree ($top)" >&2
fi

mkdir -p "$dir"
chmod 700 "$dir"
exec 9>"$dir/lock"
if ! flock -n 9; then
  echo "nrb-request: another request is open; waiting for it to finish" >&2
  flock 9
fi
# A request whose nrb-request was interrupted still runs; wait for it too.
waited=
while :; do
  state=$(systemctl --user show -P ActiveState "$unit")
  case $state in active | activating | deactivating | reloading) ;; *) break ;; esac
  [ -n "$waited" ] || echo "nrb-request: $unit is still answering an earlier request; waiting" >&2
  waited=1
  sleep 2
done

printf '%s\0' "$@" >"$dir/request"
: >"$dir/log"
systemctl --user reset-failed "$unit" 2>/dev/null || true
echo "nrb-request: asking the user to approve; this waits for their answer" >&2
systemctl --user start --wait "$unit" &
sc=$!
tail -n +1 -f --pid="$sc" "$dir/log" &
tl=$!
wait "$sc" || true
wait "$tl" || true

result=$(systemctl --user show -P Result "$unit")
status=$(systemctl --user show -P ExecMainStatus "$unit")
systemctl --user reset-failed "$unit" 2>/dev/null || true
case $result in
  success) exit 0 ;;
  exit-code) exit "$status" ;;
  *)
    echo "nrb-request: $unit ended with result $result" >&2
    exit 1
    ;;
esac
