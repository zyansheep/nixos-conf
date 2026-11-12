#!/usr/bin/env bash
# Behaviour tests for nrb-approve and nrb-request (rebuild.nix) with notify-send, gdbus, nix, the preview
# build, the switch and systemctl stubbed, in a throwaway git repo. Run at build time by rebuild.nix's
# system.checks; by hand:
#   bash nrb-approve-test.sh /nix/store/…-nrb-approve/bin/nrb-approve /nix/store/…-nrb-request/bin/nrb-request
# shellcheck disable=SC2016
set -u
approve_src=${1:?usage: nrb-approve-test.sh …/bin/nrb-approve …/bin/nrb-request}
request_src=${2:?usage: nrb-approve-test.sh …/bin/nrb-approve …/bin/nrb-request}
pass=0; fail=0
check() { # description, condition (eval'd)
  if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1"; fi
}
stub() { # name, body
  printf '#!%s\n%s\n' "$BASH" "$2" >"$ROOT/stubs/$1"; chmod +x "$ROOT/stubs/$1"
}
setup() {
  ROOT=$(mktemp -d); export ROOT
  mkdir -p "$ROOT"/{stubs,run,flake,sys-old,sys-new}
  # The stubs go ahead of the scripts' own PATH (writeShellApplication prepends its runtime inputs).
  for s in approve request; do
    src=${s}_src
    sed "s|^export PATH=\"|export PATH=\"$ROOT/stubs:|" "${!src}" >"$ROOT/$s"; chmod +x "$ROOT/$s"
  done
  git -C "$ROOT/flake" init -q
  echo a >"$ROOT/flake/flake.nix"
  git -C "$ROOT/flake" add . && git -C "$ROOT/flake" -c user.name=t -c user.email=t@t commit -qm init
  echo b >>"$ROOT/flake/flake.nix" # an uncommitted change
  : >"$ROOT/answers"; : >"$ROOT/calls"
  echo new >"$ROOT/next"
  # notify-send: questions (--wait) take the next line of answers; approve-touch also changes the repo.
  stub notify-send '
printf "notify-send" >>"$ROOT/calls"; printf " [%s]" "$@" >>"$ROOT/calls"; echo >>"$ROOT/calls"
case " $* " in *" --wait "*) ;; *) exit 0 ;; esac
a=$(head -n 1 "$ROOT/answers"); sed -i 1d "$ROOT/answers"
echo 42
case $a in
  approve) echo approve ;;
  deny) echo deny ;;
  approve-touch) echo c >"$ROOT/flake/new-file"; echo approve ;;
  close) ;;
  sleep) sleep 30 ;;
  fail) exit 1 ;;
esac'
  stub gdbus 'echo "gdbus $*" >>"$ROOT/calls"'
  stub nix 'echo "nix $*" >>"$ROOT/calls"; [ "$1 $2" = "store diff-closures" ] && printf "\033[1mfoo\033[0m: 1.0 → 2.0, +1.0 KiB\nbar: ∅ → 3\n"; true'
  # The preview build: ./result -> sys-$(next).
  stub preview '
printf "preview" >>"$ROOT/calls"; printf " [%s]" "$@" >>"$ROOT/calls"; echo >>"$ROOT/calls"
[ -e "$ROOT/preview-fail" ] && exit 3
mkdir -p "$ROOT/sys-$(cat "$ROOT/next")"; ln -sfn "$ROOT/sys-$(cat "$ROOT/next")" result'
  stub switch 'printf "switch" >>"$ROOT/calls"; printf " [%s]" "$@" >>"$ROOT/calls"; echo >>"$ROOT/calls"; [ -e "$ROOT/switch-fail" ] && exit 4; true'
  # systemctl --user start --wait runs nrb-approve in the foreground; show reports how it ended.
  stub systemctl '
case "$*" in
  "--user show -P ActiveState "*) echo inactive ;;
  "--user start --wait "*) s=0; "$ROOT/approve" || s=$?; echo "$s" >"$ROOT/unit-status" ;;
  "--user show -P Result "*) [ "$(cat "$ROOT/unit-status")" = 0 ] && echo success || echo exit-code ;;
  "--user show -P ExecMainStatus "*) cat "$ROOT/unit-status" ;;
esac
true'
  export NRB_DIR=$ROOT/run NRB_FLAKE_DIR=$ROOT/flake NRB_CURRENT=$ROOT/sys-old NRB_TIMEOUT=1
  export NRB_PREVIEW=$ROOT/stubs/preview NRB_SWITCH=$ROOT/stubs/switch
}
run_approve() { # answers…; sets st and out
  printf '%s\n' "$@" >"$ROOT/answers"
  out=$(cd / && "$ROOT/approve" 2>&1); st=$?
}
request() { # reason, args…
  printf '%s\0' "$@" >"$ROOT/run/request"
}
calls() { grep -c "^$1" "$ROOT/calls" || true; }

# Approve: switches with the args, word for word; the body shows the reason (escaped), the diff and the args.
setup; request 'fix <waybar> & more' --override-input foo 'path:/a b'
run_approve approve
check "approve exits 0 ($st)" '[ "$st" = 0 ]'
check "approve switches once with the args" '[ "$(grep ^switch "$ROOT/calls")" = "switch [--override-input] [foo] [path:/a b]" ]'
check "the preview gets the args" 'grep -qF "preview [--override-input] [foo] [path:/a b]" "$ROOT/calls"'
check "the reason is escaped" 'grep -qF "fix &lt;waybar&gt; &amp; more" "$ROOT/calls"'
check "the body lists the diff without colour" 'grep -qF "2 package(s) change:" "$ROOT/calls" && grep -qF "foo: 1.0 → 2.0" "$ROOT/calls" && ! grep -q $'"'"'\033'"'"' "$ROOT/calls"'
check "the body counts uncommitted changes" 'grep -qF "Uncommitted: 1 file changed" "$ROOT/calls"'
check "the body lists the args" 'grep -qF "Args: --override-input foo path:/a b" "$ROOT/calls"'
check "diff against the current system" 'grep -qF "nix store diff-closures $ROOT/sys-old $ROOT/sys-new" "$ROOT/calls"'
check "the repo is staged" '[ -z "$(git -C "$ROOT/flake" diff --name-only)" ]'
check "the log has the outcome" 'grep -qF "switched to $ROOT/sys-new" "$ROOT/run/log"'
check "a done notification follows" 'grep -qF "[Rebuild switched]" "$ROOT/calls"'
check "result link removed" '[ ! -e "$ROOT/run/result" ] && [ ! -L "$ROOT/run/result" ]'

# Deny and a closed notification: 77, no switch.
for a in deny close; do
  setup; request "r"; run_approve "$a"
  check "$a exits 77 ($st)" '[ "$st" = 77 ]'
  check "$a doesn't switch" '[ "$(calls switch)" = 0 ]'
done

# No answer: 75, the question is taken down, no switch.
setup; request "r"; run_approve sleep
check "timeout exits 75 ($st)" '[ "$st" = 75 ]'
check "timeout closes notification 42" 'grep -q "CloseNotification 42" "$ROOT/calls"'
check "timeout doesn't switch" '[ "$(calls switch)" = 0 ]'

# No notification daemon: 69.
setup; request "r"; run_approve fail
check "notify failure exits 69 ($st)" '[ "$st" = 69 ]'

# The preview build fails: its status, nothing asked.
setup; request "r"; touch "$ROOT/preview-fail"; run_approve approve
check "preview failure exits 3 ($st)" '[ "$st" = 3 ]'
check "preview failure asks nothing" '[ "$(calls notify-send)" = 0 ] && [ "$(calls switch)" = 0 ]'

# The build is the running system: 0, nothing asked or switched.
setup; request "r"; echo old >"$ROOT/next"; run_approve approve
check "no change exits 0 ($st)" '[ "$st" = 0 ]'
check "no change asks nothing" '[ "$(calls notify-send)" = 0 ] && [ "$(calls switch)" = 0 ]'

# The repo changes while the question is open: build and ask again, then switch.
setup; request "r"; run_approve approve-touch approve
check "changed repo exits 0 ($st)" '[ "$st" = 0 ]'
check "changed repo asks twice, saying so" '[ "$(grep -c -- "--wait" "$ROOT/calls")" = 2 ] && grep -qF "(The repo changed since the last question.) r" "$ROOT/calls"'
check "changed repo switches once" '[ "$(calls switch)" = 1 ]'

# The switch fails: its status.
setup; request "r"; touch "$ROOT/switch-fail"; run_approve approve
check "switch failure exits 4 ($st)" '[ "$st" = 4 ]'
check "switch failure notifies" 'grep -qF "[Rebuild failed (status 4)]" "$ROOT/calls"'

# A request without a reason: 64.
setup; printf '' >"$ROOT/run/request"; run_approve approve
check "no reason exits 64 ($st)" '[ "$st" = 64 ]'

# nrb-request end to end: writes the request, streams the log, exits with the unit's status.
setup; echo approve >"$ROOT/answers"
out=$(cd / && "$ROOT/request" "update foo" --show-trace 2>&1); st=$?
check "request approve exits 0 ($st)" '[ "$st" = 0 ]'
check "request streams the log" 'grep -qF "nrb-approve: switched to" <<<"$out"'
check "request passes reason and args" '[ "$(grep ^switch "$ROOT/calls")" = "switch [--show-trace]" ] && grep -qF "nrb-approve: request: update foo" <<<"$out"'
setup; echo deny >"$ROOT/answers"
out=$(cd / && "$ROOT/request" "r" 2>&1); st=$?
check "request deny exits 77 ($st)" '[ "$st" = 77 ]'
out=$(cd / && "$ROOT/request" 2>&1); st=$?
check "request without a reason exits 64 ($st)" '[ "$st" = 64 ]'
out=$(cd / && "$ROOT/request" --help 2>&1); st=$?
check "request --help exits 0 ($st)" '[ "$st" = 0 ] && grep -q "^usage: nrb-request" <<<"$out"'

echo "nrb-approve-test: $pass passed, $fail failed"
[ "$fail" = 0 ]
