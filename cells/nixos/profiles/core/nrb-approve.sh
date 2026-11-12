# nrb-approve: answers one `nrb-request` (rebuild.nix). Run by nrb-approve.service under the user's systemd
# manager, so the requesting agent's NoNewPrivileges doesn't reach the sudo below.
#
# Reads $NRB_DIR/request (NUL-separated: reason, then rebuild args), stages the repo (`git add .`, as `nrb`
# does), builds the system without root ($NRB_PREVIEW, `rebuild --build-only`) and shows a notification with
# the reason, what changes against $NRB_CURRENT (`nix store diff-closures`) and Approve / Deny. On Approve it
# runs $NRB_SWITCH (`sudo rebuild`) with the args. If the staged tree changed while the question was open, it
# builds and asks again, so what gets switched is what was shown.
#
# Exit: 0 switched, or the build is the running system already (nothing asked); 77 denied (or the
# notification was closed); 75 no answer within $NRB_TIMEOUT seconds; otherwise the status of the failed
# preview build or switch. Everything printed goes to the journal and to $NRB_DIR/log, which nrb-request
# streams to the agent.
dir=${NRB_DIR:?}
flake=${NRB_FLAKE_DIR:?}
current=${NRB_CURRENT:-/run/current-system}
timeout_s=${NRB_TIMEOUT:-900}
read -r -a preview <<<"${NRB_PREVIEW:?}"
read -r -a switch <<<"${NRB_SWITCH:?}"

exec > >(tee -a "$dir/log") 2>&1
tee_pid=$!
nid=
# shellcheck disable=SC2329 # run by the EXIT trap
cleanup() {
  # A question still on screen when the unit stops (timeout, `systemctl stop`) goes away with it.
  [ -n "$nid" ] && close_notification "$nid"
  rm -f "$dir/result" "$dir/answer"
  exec >&- 2>&-
  wait "$tee_pid" || true
}
trap cleanup EXIT
trap 'exit 143' TERM INT

say() { echo "nrb-approve: $*"; }
close_notification() {
  gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
    --method org.freedesktop.Notifications.CloseNotification "$1" >/dev/null 2>&1 || true
}
# Notification bodies are Pango markup.
esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

mapfile -d '' req <"$dir/request"
reason=${req[0]:-}
args=("${req[@]:1}")
[ -n "$reason" ] || { say "no reason in $dir/request"; exit 64; }
say "request: $reason"
[ ${#args[@]} -eq 0 ] || say "rebuild args: ${args[*]}"

cd "$flake"
note=
while :; do
  git add .
  tree=$(git write-tree)
  say "building the system without root to see what would change..."
  rm -f "$dir/result"
  status=0
  (cd "$dir" && "${preview[@]}" "${args[@]}") || status=$?
  if [ "$status" -ne 0 ]; then
    say "the build failed (status $status); nothing was asked or switched"
    exit "$status"
  fi
  new=$(readlink -f "$dir/result")
  if [ "$new" = "$(readlink -f "$current")" ]; then
    say "$new is the running system already; nothing to switch"
    exit 0
  fi

  diff=$(nix store diff-closures "$current" "$new" 2>&1 | sed 's/\x1b\[[0-9;]*m//g')
  say "changes against $current:"
  if [ -n "$diff" ]; then echo "  ${diff//$'\n'/$'\n'  }"; else echo "  (no package versions change)"; fi
  n=$(grep -c . <<<"$diff" || true)
  if [ "$n" -eq 0 ]; then
    changes="No package versions change (configuration only)."
  else
    changes="$n package(s) change:"$'\n'$(head -n 8 <<<"$diff")
    [ "$n" -le 8 ] || changes+=$'\n'"… and $((n - 8)) more"
  fi
  body="$(esc "$note$reason")"$'\n\n'"$(esc "$changes")"
  uncommitted=$(git diff --cached --shortstat HEAD)
  [ -z "$uncommitted" ] || body+=$'\n'"Uncommitted:$(esc "$uncommitted")"
  [ ${#args[@]} -eq 0 ] || body+=$'\n'"Args: $(esc "${args[*]}")"

  say "asking the user (notification, ${timeout_s}s to answer)"
  : >"$dir/answer"
  status=0
  timeout "$timeout_s" notify-send --print-id --wait --urgency=critical --app-name="NixOS rebuild" \
    --icon=system-software-update --action=approve=Approve --action=deny=Deny \
    "An agent asks to rebuild and switch" "$body" >"$dir/answer" &
  ask=$!
  # The id comes first; keep it so the question can be taken down if the unit stops.
  for _ in $(seq 50); do
    nid=$(head -n 1 "$dir/answer")
    [ -n "$nid" ] && break
    sleep 0.1
  done
  wait "$ask" || status=$?
  answer=$(sed -n 2p "$dir/answer")
  if [ "$status" -eq 124 ]; then
    close_notification "$nid"
    nid=
    say "no answer within ${timeout_s}s; not switching"
    exit 75
  fi
  nid=
  if [ "$status" -ne 0 ]; then
    say "could not show the notification (notify-send status $status); not switching"
    exit 69
  fi
  case $answer in
    approve) ;;
    *)
      say "denied by the user; not switching"
      exit 77
      ;;
  esac
  git add .
  if [ "$(git write-tree)" != "$tree" ]; then
    say "the repo changed while the question was open; building again"
    note="(The repo changed since the last question.) "
    continue
  fi
  break
done

say "approved; running ${switch[*]} ${args[*]}"
status=0
"${switch[@]}" "${args[@]}" || status=$?
if [ "$status" -eq 0 ]; then
  say "switched to $new"
  notify-send --urgency=low --app-name="NixOS rebuild" --icon=system-software-update "Rebuild switched" \
    "$(esc "$reason")" || true
else
  say "the switch failed (status $status)"
  notify-send --urgency=normal --app-name="NixOS rebuild" --icon=dialog-error "Rebuild failed (status $status)" \
    "$(esc "$reason")"$'\n'"journalctl --user -u nrb-approve" || true
fi
exit "$status"
