#!/usr/bin/env bash
# kimi-sandbox — run kimi-code (Moonshot's agentic CLI) confined with bubblewrap.
#
# The agent — and every shell command it spawns — can read & write ONLY:
#   * the current working directory (your project)
#   * /tmp
#   * kimi's own state dir  ($KIMI_CODE_HOME, default ~/.kimi-code)
# Everything else in $HOME (~/.ssh, ~/.aws, browser data, other projects) is
# invisible. The rest of the system (/nix, /etc, system tools) is read-only.
# Network stays on (kimi needs the API); the host environment is NOT inherited
# (--clearenv) so shell secrets like GITHUB_TOKEN don't leak into the agent.
#
# The kimi binary itself is the one the vendor installer dropped at
# $KIMI_CODE_HOME/bin/kimi (self-updates via `kimi upgrade`, which works from
# inside the sandbox since that dir is bound read-write and networking is on).
# Run that path directly to get an UNsandboxed kimi if ever needed.
set -euo pipefail

BWRAP="${BWRAP:-bwrap}"
KIMI_CODE_HOME="${KIMI_CODE_HOME:-$HOME/.kimi-code}"
KIMI_BIN="$KIMI_CODE_HOME/bin/kimi"
if [ ! -x "$KIMI_BIN" ]; then
  echo "kimi-sandbox: kimi binary not found at $KIMI_BIN" >&2
  echo "  Install it once with:" >&2
  echo "    curl -fsSL https://code.kimi.com/kimi-code/install.sh | KIMI_NO_MODIFY_PATH=1 bash" >&2
  exit 1
fi

workdir="$PWD"
# Binding $HOME or / as the writable workdir would expose everything and defeat
# the sandbox — refuse unless explicitly overridden.
if [ "$workdir" = "$HOME" ] || [ "$workdir" = "/" ]; then
  if [ "${KIMI_SANDBOX_ALLOW_HOME:-}" != "1" ]; then
    echo "kimi-sandbox: refusing to run with working dir '$workdir' — that would grant" >&2
    echo "  read-write access to your entire home/system and defeat the sandbox." >&2
    echo "  cd into a project directory first, or set KIMI_SANDBOX_ALLOW_HOME=1 to override." >&2
    exit 1
  fi
fi

# Isolated, persistent HOME so the agent can't read real ~ dotfiles/secrets,
# but its own login/config (under KIMI_CODE_HOME) still persists across runs.
SANDBOX_HOME="$KIMI_CODE_HOME/sandbox-home"
mkdir -p "$SANDBOX_HOME"

args=(
  --unshare-all                 # new user/pid/ipc/uts/cgroup/net namespaces...
  --share-net                   # ...but keep networking (kimi talks to its API)
  --die-with-parent
  --clearenv

  # --- read-only system: nix store, nix-ld loader, system tools, certs, dns ---
  --ro-bind /nix /nix
  --ro-bind /lib64 /lib64
  --ro-bind /run/current-system /run/current-system
  --ro-bind /etc /etc
  --ro-bind-try /run/wrappers /run/wrappers
  --ro-bind-try /usr /usr
  --ro-bind-try /bin /bin
  --proc /proc
  --dev /dev

  # --- read-write: project, scratch, kimi state, sandbox home ---
  --bind "$workdir" "$workdir"
  --bind /tmp /tmp
  --bind "$KIMI_CODE_HOME" "$KIMI_CODE_HOME"
  --bind "$SANDBOX_HOME" "$SANDBOX_HOME"
  --chdir "$workdir"

  # --- environment (allowlist; host env is cleared) ---
  --setenv HOME "$SANDBOX_HOME"
  --setenv KIMI_CODE_HOME "$KIMI_CODE_HOME"
  --setenv PATH "$KIMI_CODE_HOME/bin:/run/current-system/sw/bin:/run/wrappers/bin:/usr/bin:/bin"
  --setenv TMPDIR /tmp
  --setenv USER "${USER:-$(id -un)}"
  --setenv LOGNAME "${LOGNAME:-${USER:-$(id -un)}}"
  --setenv TERM "${TERM:-xterm-256color}"
  --setenv NIX_LD "${NIX_LD:-/run/current-system/sw/share/nix-ld/lib/ld.so}"
  --setenv NIX_LD_LIBRARY_PATH "${NIX_LD_LIBRARY_PATH:-/run/current-system/sw/share/nix-ld/lib}"
  --setenv SSL_CERT_FILE "${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"
)

# Pass through terminal/locale niceties when present.
for v in COLORTERM LANG LC_ALL LC_CTYPE LC_MESSAGES TZ; do
  if [ -n "${!v:-}" ]; then args+=(--setenv "$v" "${!v}"); fi
done

# Forward kimi/moonshot-specific config (e.g. KIMI_API_KEY) but nothing else.
while IFS='=' read -r -d '' name value; do
  case "$name" in
    KIMI_*|MOONSHOT_*) args+=(--setenv "$name" "$value") ;;
  esac
done < <(env -0)

# Read-only git identity for the agent's commits, if the user has one.
if [ -f "$HOME/.gitconfig" ]; then
  args+=(--ro-bind "$HOME/.gitconfig" "$SANDBOX_HOME/.gitconfig")
fi

# Debug/inspection hook: KIMI_SANDBOX_EXEC="cmd" runs that inside the identical
# sandbox instead of kimi (useful to verify the confinement).
if [ -n "${KIMI_SANDBOX_EXEC:-}" ]; then
  exec "$BWRAP" "${args[@]}" -- /run/current-system/sw/bin/bash -c "$KIMI_SANDBOX_EXEC"
fi

exec "$BWRAP" "${args[@]}" -- "$KIMI_BIN" "$@"
