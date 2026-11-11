# CLIProxyAPI: one hub that holds the Claude subscriptions' sign-ins and serves
# them to Claude Code on every machine (Theo's setup, his gist:
# https://gist.github.com/t3dotgg/59a521d2ee8d70aaf90e280c175072af).
#
# - Runs as zyansheep (a user unit) on 127.0.0.1:8317 only; Caddy puts it on the
#   tailnet as https://cliproxy.tn.zyancraft.net (tailnet-https.nix). The
#   firewall is off on this host, so never let it bind anything else.
# - State in ~/.local/share/cliproxyapi: config.yaml (written once, below; the
#   server hashes the management key into it on its first start), auths/ (one
#   JSON per signed-in account) and keys.env (mode 0600): CLIPROXY_URL,
#   CLIPROXY_MANAGEMENT_KEY and CLIPROXY_API_KEY in plain text. Clients send the
#   API key; dither-accounts' cliproxy provider uses the management key.
# - Accounts are signed in by dither-accounts (its `claude` kind and `cliproxy`
#   provider, ~/Projects/dither-accounts/docs/cliproxy.md), not by hand.
# - Theo's two rules: the Claude subscriptions only ever serve Claude Code, and
#   only our own traffic.
# - Upgrading: change `version`, set both hashes to lib.fakeHash and build; the
#   errors give the real ones.
_: {
  lib,
  pkgs,
  ...
}: let
  version = "8.0.67";
  # go.mod wants go >= 1.26.10; this nixpkgs has 1.26.8, and 1.27.
  cliproxyapi = (pkgs.buildGoModule.override {go = pkgs.go_1_27;}) {
    pname = "cliproxyapi";
    inherit version;
    src = pkgs.fetchFromGitHub {
      owner = "router-for-me";
      repo = "CLIProxyAPI";
      rev = "v${version}";
      hash = "sha256-njk9i5JbHvJ3lTB7Bn0wTxFtmcJ5MEokgdOHNhzDPPY=";
    };
    vendorHash = "sha256-02Qqrs9lQ0kE+Tnk/SWvlkLXGSh8RQPxRawFZogRYNc=";
    subPackages = ["cmd/server"];
    ldflags = [
      "-s"
      "-w"
      "-X main.Version=${version}"
      "-X main.Commit=v${version}"
      "-X main.BuildDate=1970-01-01"
    ];
    postInstall = "mv $out/bin/server $out/bin/cliproxyapi";
    meta = {
      description = "Proxy that serves Claude/Codex subscriptions as one API";
      homepage = "https://github.com/router-for-me/CLIProxyAPI";
      license = lib.licenses.mit;
      mainProgram = "cliproxyapi";
    };
  };

  # Written only when config.yaml is missing, so the server's own edits (the
  # hashed key, changes made through the management API) are kept.
  init = pkgs.writeShellScript "cliproxyapi-init" ''
    set -eu
    umask 077
    dir="$HOME/.local/share/cliproxyapi"
    mkdir -p "$dir/auths"
    [ -e "$dir/config.yaml" ] && exit 0
    key() { ${pkgs.coreutils}/bin/head -c 32 /dev/urandom | ${pkgs.coreutils}/bin/base64 | ${pkgs.coreutils}/bin/tr -d '/+=\n'; }
    mgmt="$(key)"
    access="sk-$(key)"
    cat > "$dir/keys.env.tmp" <<EOF
    CLIPROXY_URL=http://127.0.0.1:8317
    CLIPROXY_MANAGEMENT_KEY=$mgmt
    CLIPROXY_API_KEY=$access
    EOF
    mv "$dir/keys.env.tmp" "$dir/keys.env"
    cat > "$dir/config.yaml.tmp" <<EOF
    config-version: 8
    server:
      host: "127.0.0.1"
      port: 8317
      # Caddy, on this machine, sends the tailnet client's address.
      trusted-proxies: ["127.0.0.1", "::1"]
      discovery:
        enabled: false
    management:
      # Tailnet only (Caddy), and the key is still required.
      allow-remote: true
      secret-key: "$mgmt"
    access:
      api-keys: ["$access"]
    routing:
      # dither-accounts sets each credential's priority so the account whose
      # weekly limit resets soonest goes first; ties take turns.
      strategy: "round-robin"
      # Prompt caches are per account: keep a session on its account.
      session-affinity: true
      session-affinity-ttl: "1h"
      retry:
        request-retry: 3
        max-retry-interval: 30
    oauth:
      auth-dir: "$dir/auths"
    observability:
      usage:
        usage-statistics-enabled: true
    EOF
    mv "$dir/config.yaml.tmp" "$dir/config.yaml"
  '';
in {
  systemd.user.services.cliproxyapi = {
    description = "CLIProxyAPI (pooled Claude subscriptions for Claude Code)";
    unitConfig.ConditionUser = "zyansheep";
    after = ["network-online.target"];
    wantedBy = ["default.target"];
    serviceConfig = {
      ExecStartPre = "${init}";
      ExecStart = "${cliproxyapi}/bin/cliproxyapi -config %h/.local/share/cliproxyapi/config.yaml";
      # "-": the init step makes it on the first start.
      WorkingDirectory = "-%h/.local/share/cliproxyapi";
      Restart = "always";
      RestartSec = 5;
    };
  };
}
