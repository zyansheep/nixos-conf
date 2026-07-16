{
  inputs,
  common,
}:
{
  lib,
  config,
  pkgs,
  ...
}:
let
  conf = "/etc/openfortivpn/hampshire.conf";
  cookieFile = "/run/hampshire-vpn.cookie";

  # Runs as ExecStartPost, so the unit reports `activating` (slider animates)
  # until the tunnel is actually usable, and a start that never produces a
  # working ppp0 ends up `failed` (red slider) instead of silently half-up.
  waitForTunnel = pkgs.writeShellScript "hampshire-vpn-wait" ''

    for _ in $(${pkgs.coreutils}/bin/seq 1 40); do
      if ${pkgs.iproute2}/bin/ip -4 addr show dev ppp0 2>/dev/null | ${pkgs.gnugrep}/bin/grep -q inet; then
        # The gateway's split-tunnel routes push 45.55.x / 206.107.x but OMIT
        # 192.33.12.0/24 (thehub + other legacy-block services); add it here.
        # It disappears with ppp0 on teardown, so no down-side cleanup needed.
        ${pkgs.iproute2}/bin/ip route replace 192.33.12.0/24 dev ppp0
        ${pkgs.coreutils}/bin/rm -f ${cookieFile}
        exit 0
      fi
      # openfortivpn already gone -> cookie rejected / TLS error / pppd died.
      kill -0 "$MAINPID" 2>/dev/null || exit 1
      ${pkgs.coreutils}/bin/sleep 0.5
    done
    exit 1
  '';

  # Root control wrapper for the Hampshire (Fortinet SSL VPN, Azure SAML SSO).
  # This is the ONLY thing the passwordless sudo-rs rule below grants: it can
  # feed a cookie to the unit and start/stop it, nothing else. Kept in the nix
  # store so the rule targets an immutable path, not a user-writable script.
  hampshire-vpn = pkgs.writeShellScriptBin "hampshire-vpn" ''
    set -euo pipefail
    case "''${1:-}" in
      connect)
        # stdin (the SVPNCOOKIE extracted in the user session) -> root-only
        # file; the unit reads it via StandardInput=file: and deletes it once
        # the tunnel is up. Never on a command line, never user-readable.
        umask 077
        ${pkgs.coreutils}/bin/cat > ${cookieFile}
        # --no-block: state flows to the eww poll via `systemctl is-active`;
        # restart (not start) so a previously failed attempt retries cleanly.
        exec ${pkgs.systemd}/bin/systemctl restart --no-block hampshire-vpn.service
        ;;
      down)
        exec ${pkgs.systemd}/bin/systemctl stop --no-block hampshire-vpn.service
        ;;
      up)
        # Manual/debug fallback OUTSIDE the unit: SAML browser-redirect
        # connect in the foreground. This gateway won't do the localhost
        # redirect reliably, which is why the toggle uses `connect` instead.
        exec ${pkgs.openfortivpn}/bin/openfortivpn -c ${conf} --saml-login
        ;;
      *)
        echo "usage: hampshire-vpn {connect|down|up}" >&2
        exit 1
        ;;
    esac
  '';
in
{
  environment.systemPackages = [ hampshire-vpn ];

  # The tunnel as a real unit: `systemctl is-active` is the single source of
  # truth for the eww slider (inactive/activating/active/deactivating/failed —
  # exactly the switch CSS classes), every attempt's openfortivpn output lands
  # in `journalctl -u hampshire-vpn`, and there is no hand-rolled pgrep/state-
  # file supervision. On-demand only: started/stopped via the wrapper above.
  #
  # The conf itself (host/port/trusted-cert/set-dns) lives at
  # /etc/openfortivpn/hampshire.conf, persisted via impermanence (see
  # hosts/isomorph/default.nix) and intentionally NOT in this public repo.
  systemd.services.hampshire-vpn = {
    description = "Hampshire SSL-VPN tunnel (openfortivpn, portal-cookie auth)";
    # A nixos-rebuild while the VPN is up must not bounce the tunnel — the
    # cookie file is already deleted by then, so a restart would just fail.
    restartIfChanged = false;
    serviceConfig = {
      Type = "exec";
      ExecStart = "${pkgs.openfortivpn}/bin/openfortivpn -c ${conf} --cookie-on-stdin";
      StandardInput = "file:${cookieFile}";
      ExecStartPost = "${waitForTunnel}";
      ExecStopPost = "${pkgs.coreutils}/bin/rm -f ${cookieFile}";
      KillSignal = "SIGINT"; # openfortivpn's clean-teardown signal
      Restart = "no";
    };
  };

  # Let zyansheep toggle the VPN from the eww popup with no password prompt —
  # an eww button has no controlling TTY to type one into. Scoped to ONLY the
  # wrapper above. Mirrors, in spirit, the polkit rule that already lets the
  # same popup toggle tailscaled. isomorph uses sudo-rs.
  security.sudo-rs.extraRules = [
    {
      users = [ "zyansheep" ];
      commands = [
        {
          command = "/run/current-system/sw/bin/hampshire-vpn";
          options = [ "NOPASSWD" ];
        }
      ];
    }
  ];
}
