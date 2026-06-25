{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}: let
  # Root control wrapper for the Hampshire (Fortinet SSL VPN, Azure SAML SSO).
  # openfortivpn needs root for the tun device + route table; the SAML browser
  # step is handled separately in the user session (it can't reach the user's
  # browser from a root context). Kept in the nix store so the passwordless
  # sudo-rs rule below can target an immutable path, not a user-writable script.
  #
  # The conf itself (host/port/trusted-cert/set-dns) lives at
  # /etc/openfortivpn/hampshire.conf, which is persisted via impermanence
  # (see hosts/isomorph/default.nix) and intentionally NOT in this public repo.
  hampshire-vpn = pkgs.writeShellScriptBin "hampshire-vpn" ''
    set -uo pipefail
    case "''${1:-}" in
      up)
        # openfortivpn already has pppd hardcoded in its closure, so no PATH
        # setup is needed. It prints the SAML auth URL and blocks on the local
        # callback listener; the user-session toggle opens that URL.
        exec ${pkgs.openfortivpn}/bin/openfortivpn \
          -c /etc/openfortivpn/hampshire.conf --saml-login
        ;;
      down)
        exec ${pkgs.procps}/bin/pkill -INT -x openfortivpn
        ;;
      status)
        if ${pkgs.procps}/bin/pgrep -x openfortivpn >/dev/null 2>&1; then
          echo active
        else
          echo inactive
        fi
        ;;
      *)
        echo "usage: hampshire-vpn {up|down|status}" >&2
        exit 1
        ;;
    esac
  '';
in {
  environment.systemPackages = [hampshire-vpn];

  # Let zyansheep toggle the VPN from the waybar/eww popup with no password
  # prompt — an eww button has no controlling TTY to type one into. Scoped to
  # ONLY this wrapper (which only ever runs openfortivpn / pkill openfortivpn),
  # so it is not a blanket root grant. Mirrors, in spirit, the polkit rule that
  # already lets the same popup toggle tailscaled. isomorph uses sudo-rs.
  security.sudo-rs.extraRules = [
    {
      users = ["zyansheep"];
      commands = [
        {
          command = "/run/current-system/sw/bin/hampshire-vpn";
          options = ["NOPASSWD"];
        }
      ];
    }
  ];
}
