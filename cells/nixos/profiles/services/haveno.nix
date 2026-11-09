# Haveno (Reto network) headless daemon: the Monero P2P exchange node.
# Moved here from sguru on 2026-11-08.
#
# - Runs as zyansheep with the desktop app's default data dir
#   (~/.local/share/Haveno-reto), so the same account could be opened in the
#   desktop app instead. Never run both at once: same keys, same wallet.
# - The account is password protected and the daemon can't open it by itself.
#   Open it from a client (the Haveno Android app over the onion below), or:
#     grpcurl -plaintext -H "password:$HAVENO_API_PASSWORD" \
#       -import-path <haveno proto dir> -proto grpc.proto \
#       -d '{"password":"..."}' localhost:1201 io.haveno.protobuffer.Account/OpenAccount
# - Secrets live outside the repo and the store, in ~/.config/haveno/:
#   daemon.env (HAVENO_API_PASSWORD=..., mode 0600) and onion/hs_ed25519_secret_key
#   (the key behind the API's onion address, carried over from sguru so paired
#   clients keep working).
# - P2P traffic goes through Haveno's own bundled Tor. The system Tor below
#   only publishes the API onion service. The bundled tor and monero-wallet-rpc
#   are prebuilt binaries that run through nix-ld.
# - The firewall is off on this host, so a oneshot limits the API port to
#   loopback and the tailnet (same rules sguru had).
# - Upgrading: change `version` and `hash` (from the release's .hashes file,
#   whose signature checks against Reto's key, fingerprint
#   DAA2 4D87 8B8D 36C9 0120 A897 CA02 DAC1 2DAE 2D0F). Back up the data dir
#   first; newer versions migrate it and there's no going back.
{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}: let
  version = "1.8.0";
  jar = pkgs.fetchurl {
    url = "https://github.com/retoaccess1/haveno-reto/releases/download/v${version}-reto/daemon-linux-x86_64.jar";
    hash = "sha256-9ZwHhq1g3M4nNW6zE7/17rcRfevisnHujO6RBE9F3L4=";
  };

  # Not in nixpkgs. Upstream's static release binary (identical to sguru's).
  grpcwebproxy = pkgs.stdenvNoCC.mkDerivation {
    pname = "grpcwebproxy";
    version = "0.15.0";
    src = pkgs.fetchzip {
      url = "https://github.com/improbable-eng/grpc-web/releases/download/v0.15.0/grpcwebproxy-v0.15.0-linux-x86_64.zip";
      sha256 = "1sx2cx748v26c1dhl6kbxyrs9w0qsxqym2yv4lzb8m24mh038nsm";
    };
    installPhase = "install -Dm755 grpcwebproxy-v0.15.0-linux-x86_64 $out/bin/grpcwebproxy";
  };

  user = "zyansheep";
  home = "/home/${user}";
  configDir = "${home}/.config/haveno";
  apiPort = 1201;
  grpcWebPort = 2134;

  iptables = "${pkgs.iptables}/bin/iptables";
  ip6tables = "${pkgs.iptables}/bin/ip6tables";
  firewallRules = [
    "-p tcp --dport ${toString apiPort} -i lo -j ACCEPT"
    "-p tcp --dport ${toString apiPort} -i tailscale0 -j ACCEPT"
    "-p tcp --dport ${toString apiPort} -j REJECT --reject-with tcp-reset"
  ];
  ruleScript = verb:
    lib.concatMapStrings (r: ''
      ${iptables} -C INPUT ${r} 2>/dev/null ${
        if verb == "add"
        then "|| ${iptables} -A INPUT ${r}"
        else "&& ${iptables} -D INPUT ${r}"
      }
    '')
    firewallRules
    + ''
      ${ip6tables} -C INPUT -p tcp --dport ${toString apiPort} -i lo -j ACCEPT 2>/dev/null ${
        if verb == "add"
        then "|| ${ip6tables} -A INPUT -p tcp --dport ${toString apiPort} -i lo -j ACCEPT"
        else "&& ${ip6tables} -D INPUT -p tcp --dport ${toString apiPort} -i lo -j ACCEPT"
      }
      ${ip6tables} -C INPUT -p tcp --dport ${toString apiPort} -j REJECT --reject-with tcp-reset 2>/dev/null ${
        if verb == "add"
        then "|| ${ip6tables} -A INPUT -p tcp --dport ${toString apiPort} -j REJECT --reject-with tcp-reset"
        else "&& ${ip6tables} -D INPUT -p tcp --dport ${toString apiPort} -j REJECT --reject-with tcp-reset"
      }
      true
    '';
in {
  systemd.services.haveno-firewall = {
    description = "Limit the Haveno API port to loopback and the tailnet";
    wantedBy = ["multi-user.target"];
    before = ["haveno.service"];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = pkgs.writeShellScript "haveno-firewall-up" (ruleScript "add");
      ExecStop = pkgs.writeShellScript "haveno-firewall-down" (ruleScript "del");
    };
  };

  systemd.services.haveno = {
    description = "Haveno (Reto) headless daemon";
    after = ["network-online.target" "haveno-firewall.service"];
    wants = ["network-online.target"];
    requires = ["haveno-firewall.service"];
    wantedBy = ["multi-user.target"];
    # The bundled Tor launcher shells out to `ps`.
    path = [pkgs.procps];
    # The bundled tor and monero-wallet-rpc need nix-ld, whose variables only
    # reach login shells.
    environment = {
      inherit (config.environment.variables) NIX_LD NIX_LD_LIBRARY_PATH;
      HOME = home;
    };
    serviceConfig = {
      User = user;
      Group = "users";
      EnvironmentFile = "${configDir}/daemon.env";
      WorkingDirectory = home;
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.jdk21_headless}/bin/java -Xms256m -Xmx1200m -jar ${jar}"
        "--baseCurrencyNetwork=XMR_MAINNET"
        "--useLocalhostForP2P=false"
        "--useDevPrivilegeKeys=false"
        "--nodePort=9999"
        "--appName=Haveno-reto"
        "--appDataDir=${home}/.local/share/Haveno-reto"
        "--apiPort=${toString apiPort}"
        "--apiPassword=\${HAVENO_API_PASSWORD}"
        "--useNativeXmrWallet=false"
        "--ignoreLocalXmrNode=false"
        "--disableRateLimits=true"
        "--logLevel=INFO"
      ];
      Restart = "on-failure";
      RestartSec = 15;
      TimeoutStopSec = 120;
      KillSignal = "SIGTERM";
      MemoryMax = "2200M";
      NoNewPrivileges = true;
      PrivateTmp = true;
    };
  };

  # grpc-web front for the API, used by the Haveno Android app over the onion.
  systemd.services.haveno-grpcweb = {
    description = "grpc-web proxy for the Haveno API";
    after = ["haveno.service"];
    wants = ["haveno.service"];
    wantedBy = ["multi-user.target"];
    serviceConfig = {
      DynamicUser = true;
      ExecStart = lib.concatStringsSep " " [
        "${grpcwebproxy}/bin/grpcwebproxy"
        "--backend_addr=127.0.0.1:${toString apiPort} --backend_tls=false"
        "--run_tls_server=false --run_http_server=true"
        "--server_bind_address=127.0.0.1 --server_http_debug_port=${toString grpcWebPort}"
        "--server_http_max_read_timeout=2h --server_http_max_write_timeout=2h"
        "--backend_max_call_recv_msg_size=67108864"
      ];
      Restart = "on-failure";
      RestartSec = 5;
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
    };
  };

  # The API's onion address (kb5qb6lh…yeeandad.onion), same as it was on sguru.
  services.tor = {
    enable = true;
    relay.onionServices.haveno-api = {
      version = 3;
      secretKey = "${configDir}/onion/hs_ed25519_secret_key";
      map = [
        {
          port = apiPort;
          target = {
            addr = "127.0.0.1";
            port = apiPort;
          };
        }
        {
          port = 80;
          target = {
            addr = "127.0.0.1";
            port = apiPort;
          };
        }
        {
          port = grpcWebPort;
          target = {
            addr = "127.0.0.1";
            port = grpcWebPort;
          };
        }
      ];
    };
  };
}
