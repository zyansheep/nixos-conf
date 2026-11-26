# Traffic for this machine's own tailnet addresses is taken only from tailscale0 (and loopback).
#
# - The firewall is off fleet-wide, and Linux accepts a packet for any of its addresses on any interface. So a
#   device on the home network that adds one route (this machine's tailnet address via its LAN address) reaches every
#   service bound only to the tailnet address (the dev servers, disp-files, T3's login, Caddy for *.tn) as if it
#   were on the tailnet. Tailscale's own rules drop forged tailnet *sources* off tailscale0, not this.
# - A rule in the raw table, ahead of conntrack and of Tailscale's ts-input chain, and matched only for addresses
#   this machine owns (addrtype LOCAL), so routing to other tailnet hosts is untouched.
# - A container on a bridge that dials this machine's tailnet address is refused too: give it the bridge gateway
#   or loopback instead.
# - Check: `sudo iptables -t raw -S tailnet-guard`; the DROP counter in `-L tailnet-guard -v` counts what it caught.
_: {pkgs, ...}: let
  chain = "tailnet-guard";
  families = [
    {
      ipt = "${pkgs.iptables}/bin/iptables";
      net = "100.64.0.0/10";
    }
    {
      ipt = "${pkgs.iptables}/bin/ip6tables";
      net = "fd7a:115c:a1e0::/48";
    }
  ];
  jump = net: "PREROUTING -d ${net} -m addrtype --dst-type LOCAL -j ${chain}";
  up = pkgs.writeShellScript "tailnet-guard-up" (pkgs.lib.concatMapStrings ({
      ipt,
      net,
    }: ''
      ${ipt} -w -t raw -N ${chain} 2>/dev/null || ${ipt} -w -t raw -F ${chain}
      ${ipt} -w -t raw -A ${chain} -i tailscale0 -j RETURN
      ${ipt} -w -t raw -A ${chain} -i lo -j RETURN
      ${ipt} -w -t raw -A ${chain} -j DROP
      ${ipt} -w -t raw -C ${jump net} 2>/dev/null || ${ipt} -w -t raw -I ${jump net}
    '')
    families);
  down = pkgs.writeShellScript "tailnet-guard-down" (pkgs.lib.concatMapStrings ({
      ipt,
      net,
    }: ''
      while ${ipt} -w -t raw -D ${jump net} 2>/dev/null; do :; done
      ${ipt} -w -t raw -F ${chain} 2>/dev/null || true
      ${ipt} -w -t raw -X ${chain} 2>/dev/null || true
    '')
    families);
in {
  systemd.services.tailnet-guard = {
    description = "Take traffic for this machine's tailnet addresses only from tailscale0";
    wantedBy = ["sysinit.target"];
    wants = ["network-pre.target"];
    before = ["network-pre.target" "tailscaled.service"];
    after = ["systemd-modules-load.service"];
    unitConfig.DefaultDependencies = false;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${up}";
      ExecStop = "${down}";
    };
  };
}
