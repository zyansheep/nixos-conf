# Haveno (Reto network) headless daemon: the Monero P2P exchange node.
# Moved here from sguru on 2026-11-08.
#
# - Runs as zyansheep with the desktop app's default data dir
#   (~/.local/share/Haveno-reto), sandboxed by systemd to that dir.
# - `haveno-desktop` (on PATH) is the desktop app, same version, on the same
#   data: dither-accounts' haveno kind runs it in a sandbox with a live view
#   (~/Projects/dither-accounts/docs/haveno.md). The daemon and the app share
#   a flock on <data>/.instance.lock, so only one runs; the daemon restarts
#   after any exit and waits on the lock while the app is open.
# - The account is password protected and the daemon can't open it by itself.
#   dither-accounts' haveno account unlocks it (and again after restarts), or
#   a client (the Haveno Android app over the onion below), or:
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
  version = "1.12.0";
  jar = pkgs.fetchurl {
    url = "https://github.com/retoaccess1/haveno-reto/releases/download/v${version}-reto/daemon-linux-x86_64.jar";
    hash = "sha256-0LcXGtXHofM2rhpyMrDScEK8tUDAGhpbMTPcIVIi0Qc=";
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

  # The desktop app, same version: the fat jar out of Reto's signed .deb (its
  # .deb.sig checks against the key above; the .hashes file lists the .deb's
  # hash under a stale 1.10.0 name). It bundles JavaFX with its natives, which
  # want GTK, X11 and GL on the library path.
  desktopJar = pkgs.stdenvNoCC.mkDerivation {
    pname = "haveno-reto-desktop-jar";
    inherit version;
    src = pkgs.fetchurl {
      url = "https://github.com/retoaccess1/haveno-reto/releases/download/v${version}-reto/haveno-v${version}-linux-x86_64-installer.deb";
      hash = "sha256-ozGvdP9Ob4oxC81ilkPgqzq0ycd/rvihXNy/N1dLwYk=";
    };
    nativeBuildInputs = [pkgs.dpkg];
    unpackPhase = "dpkg-deb -x $src .";
    installPhase = ''
      install -Dm644 opt/haveno/lib/app/desktop-${version}-SNAPSHOT-all.jar $out/share/haveno/desktop.jar
      grep -o -- '--add-opens=[^ ]*' opt/haveno/lib/app/Haveno.cfg > $out/share/haveno/java-options
    '';
  };
  guiLibs = with pkgs; [gtk3 glib pango cairo gdk-pixbuf atk harfbuzz freetype fontconfig libGL xorg.libX11 xorg.libXtst xorg.libXxf86vm xorg.libXext xorg.libXrender xorg.libXi xorg.libXrandr xorg.libXcursor xorg.libXinerama alsa-lib];

  # What nix-ld needs, as store paths: Haveno unpacks prebuilt tor and
  # monero-wallet-rpc and runs them, and inside a sandbox (dither-accounts)
  # /run/current-system is not there. /lib64 (nix-ld's shim) must be.
  nixLdLibs = pkgs.buildEnv {
    name = "haveno-nix-ld-libs";
    paths = config.programs.nix-ld.libraries;
    pathsToLink = ["/lib"];
  };
  nixLdEnv = {
    NIX_LD = pkgs.stdenv.cc.bintools.dynamicLinker;
    NIX_LD_LIBRARY_PATH = "${nixLdLibs}/lib";
  };

  # One of the daemon and the desktop app at a time, on one data dir: both run
  # under flock on the same file. The desktop waits for the lock (the
  # dither-accounts haveno kind stops the daemon first); the daemon waits for
  # the desktop to quit and then comes back by itself (Restart=always).
  lockFile = data: "${data}/.instance.lock";

  # `haveno-desktop [args]`: the GUI on $HAVENO_DATA_DIR (default the same dir
  # as the daemon). Used by dither-accounts' haveno kind in its sandbox.
  havenoDesktop = pkgs.writeShellScriptBin "haveno-desktop" ''
    set -eu
    data="''${HAVENO_DATA_DIR:-$HOME/.local/share/Haveno-reto}"
    mkdir -p "$data"
    export PATH=${lib.makeBinPath [pkgs.procps pkgs.coreutils]}:$PATH
    export LD_LIBRARY_PATH=${lib.makeLibraryPath guiLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    export NIX_LD=${nixLdEnv.NIX_LD} NIX_LD_LIBRARY_PATH=${nixLdEnv.NIX_LD_LIBRARY_PATH}
    exec ${pkgs.util-linux}/bin/flock -w "''${HAVENO_LOCK_WAIT:-120}" ${lockFile "\"$data\""} \
      ${pkgs.jdk21}/bin/java -XX:MaxRAM=4g -XX:+UseG1GC -XX:+ExitOnOutOfMemoryError -Xss1280k \
      -Dfile.encoding=UTF-8 $(cat ${desktopJar}/share/haveno/java-options) \
      -cp ${desktopJar}/share/haveno/desktop.jar haveno.desktop.app.HavenoAppMain \
      --baseCurrencyNetwork=XMR_MAINNET --appName=Haveno-reto --appDataDir="$data" "$@"
  '';

  user = "zyansheep";
  home = "/home/${user}";
  dataDir = "${home}/.local/share/Haveno-reto";
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
    # Comes back after any exit, including a clean stop through the API (how
    # the desktop app takes over); waits on the lock while the desktop runs.
    startLimitIntervalSec = 0;
    # The bundled Tor launcher shells out to `ps`.
    path = [pkgs.procps];
    environment = nixLdEnv // {HOME = home;};
    serviceConfig = {
      User = user;
      Group = "users";
      EnvironmentFile = "${configDir}/daemon.env";
      WorkingDirectory = dataDir;
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.util-linux}/bin/flock ${lockFile dataDir}"
        "${pkgs.jdk21_headless}/bin/java -Xms256m -Xmx1200m -jar ${jar}"
        "--baseCurrencyNetwork=XMR_MAINNET"
        "--useLocalhostForP2P=false"
        "--useDevPrivilegeKeys=false"
        "--nodePort=9999"
        "--appName=Haveno-reto"
        "--appDataDir=${dataDir}"
        "--apiPort=${toString apiPort}"
        "--apiPassword=\${HAVENO_API_PASSWORD}"
        "--useNativeXmrWallet=false"
        "--ignoreLocalXmrNode=false"
        "--disableRateLimits=true"
        "--logLevel=INFO"
      ];
      Restart = "always";
      RestartSec = 15;
      TimeoutStopSec = 120;
      KillSignal = "SIGTERM";
      MemoryMax = "2200M";

      # Sandbox: it sees its data dir and nothing else of the home, writes
      # nowhere else, and has no privileges or devices.
      ProtectHome = "tmpfs";
      BindPaths = [dataDir];
      ProtectSystem = "strict";
      PrivateTmp = true;
      PrivateDevices = true;
      NoNewPrivileges = true;
      CapabilityBoundingSet = "";
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectKernelLogs = true;
      ProtectControlGroups = true;
      ProtectClock = true;
      ProtectHostname = true;
      ProtectProc = "invisible";
      RestrictSUIDSGID = true;
      RestrictRealtime = true;
      RestrictNamespaces = true;
      LockPersonality = true;
      SystemCallArchitectures = "native";
      RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK"];
      UMask = "0077";
    };
  };

  environment.systemPackages = [havenoDesktop];

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
