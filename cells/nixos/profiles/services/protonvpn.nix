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
  # DNS ownership while the tunnel is up. This host has NO systemd-resolved;
  # /etc/resolv.conf is written by resolvconf and is currently owned by
  # tailscale (MagicDNS 100.100.100.100 + search ts.zyancraft.net).
  #
  #   false (default) -> NM never touches resolv.conf. Nothing that works today
  #                      breaks, but DNS keeps going to MagicDNS, i.e. queries
  #                      do NOT go through Proton: a DNS leak.
  #   true            -> NM installs Proton's resolvers while connected. Closes
  #                      the leak, but MagicDNS stops resolving, so tailnet
  #                      hosts are reachable by IP (100.64.0.1) and not by name.
  #
  # Defaulting to false because it is the option that cannot break a working
  # machine, and this profile cannot be tested before it is switched. Flip it
  # once the tunnel is verified up — see the resilience notes in the README of
  # this change / the comments below.
  protonOwnsDns = false;
in
{
  # The official Proton client, rather than a hand-maintained wg-quick config.
  #
  # WHY: Proton VPN credentials are certificate-backed and expire.
  # proton-vpn-api-core ships a CertificateRefresher on a REFRESH_INTERVAL of
  # 7 days (+/-22%, see session/credentials.py). A static .conf downloaded from
  # the web UI has no refresher, so it would need re-downloading on that
  # cadence AND would fail silently: wg-quick keeps the interface up with a
  # dead certificate, so `systemctl is-active` would report a healthy tunnel
  # that passes no traffic.
  #
  # The cost of this choice is NetworkManager. proton.vpn.backend.networkmanager
  # is the client's ONLY backend -- openvpn, protun and wireguard all drive
  # NM over D-Bus -- so there is no way to use the official client without it.
  networking.networkmanager = {
    enable = true;

    # NM's default wifi backend is wpa_supplicant, and enabling that trips the
    # "only one wireless daemon" assertion against iwd. NM manages no wifi
    # device here anyway (see `unmanaged`), so this is purely about not
    # starting a second supplicant alongside iwd.
    wifi.backend = "iwd";

    # isomorph is an iwd machine (networking.wireless.iwd, with iwd doing its
    # own DHCP via General.EnableNetworkConfiguration). NM is enabled here
    # purely to own the VPN interface Proton creates, so it is told to manage
    # NOTHING except wireguard/tun devices.
    #
    # `*` + `except:` rather than listing wlan*/tailscale0/ppp0: an explicit
    # deny-list silently starts managing any NEW interface (a USB ethernet
    # dongle, a dock), which would then fight dhcpcd for it. This inverts that
    # default so anything unforeseen stays with its existing owner.
    #
    # WHY not simply hand wifi to NM (wifi.backend = "iwd"): this change cannot
    # be tested before it is switched, and the failure modes are not
    # symmetric. Misconfigured here you lose the VPN. With NM owning wlan, a
    # bad switch costs you wifi on a laptop -- and iwd's
    # EnableNetworkConfiguration would have to be flipped in the same switch,
    # so there is no one-line way back.
    unmanaged = [
      "*"
      "except:type:wireguard" # protun / wireguard protocols
      "except:type:tun" # openvpn protocol
    ];

    dns = lib.mkIf (!protonOwnsDns) "none";
  };

  # The NM module defaults this to false because NM normally runs DHCP itself.
  # It manages no physical interface here (see `unmanaged`), so the host's
  # dhcpcd must stay exactly as it was before this profile existed.
  networking.useDHCP = lib.mkForce true;

  environment.systemPackages = with pkgs; [
    proton-vpn-cli # `protonvpn`: signin/connect/disconnect/status/servers/...
    proton-vpn # GTK app, for browsing servers and Secure Core
  ];

  # NM's polkit rules let this group manage connections without a password, so
  # the waybar/eww toggle needs no sudo wrapper at all -- unlike hampshire-vpn,
  # where openfortivpn genuinely needs root.
  users.users.zyansheep.extraGroups = [ "networkmanager" ];
}
