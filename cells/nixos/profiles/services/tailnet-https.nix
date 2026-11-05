# HTTPS for everything served over the tailnet, behind one Caddy on this machine.
# To add a service, add a line to `services`. Backends can be on any machine.
#
# - One wildcard certificate for *.tn.zyancraft.net, from Let's Encrypt over
#   DNS-01 through Njalla's API. The token is in a root-only file that is never
#   in the repo or the store. It can only touch TXT records at
#   _acme-challenge.tn, so a leak gets certificates for these names and no more.
#   To replace it, make one with the same limits through Njalla's add-token
#   (allowed_domains zyancraft.net; allowed_methods add-record, remove-record,
#   list-records; allowed_types TXT; allowed_prefixes _acme-challenge.tn) and
#   write NJALLA_TOKEN=<token> to that file, mode 0400.
# - Public DNS has *.tn -> this machine's tailnet addresses, so the names only
#   lead anywhere from inside the tailnet. They can't sit under
#   ts.zyancraft.net: MagicDNS answers NXDOMAIN for names it doesn't know there.
# - Caddy binds the tailnet addresses only, and the IP filter holds it to tailnet
#   peers (the firewall is off fleet-wide). Without the filter, a LAN host routing
#   to 100.64.0.4 could reach tailnet-only backends through Caddy, which would
#   look like a tailnet client to them.
# - ip_nonlocal_bind lets Caddy bind before tailscale0 is up. Its unit doesn't
#   restart on exit code 1, so a failed bind at boot would leave it down.
_: {lib, ...}: let
  domain = "tn.zyancraft.net";
  services = {
    t3 = "100.64.0.4:3773";
    t3-isomorph = "100.64.0.2:3773";
    t3-sguru = "100.64.0.1:3773";
    t3-scry = "100.64.0.3:3000";
    disp = "100.64.0.4:5174"; # the name has to be in DISP_DEV_HOSTS
    disp-files = "100.64.0.4:5180";
    verified-ui = "100.64.0.4:5188";
    finance = "100.64.0.4:5191"; # ~/Projects/FinanceTracker portal (finance-portal user unit)
    # The tailnet address, not loopback: the hub trusts loopback with /adapter.
    chat = "100.64.0.4:8460";
    calendar = "100.64.0.4:8470"; # ~/Projects/dither-calendar (dither-calendar user unit)
  };
in {
  security.acme = {
    acceptTerms = true;
    certs.${domain} = {
      domain = "*.${domain}";
      dnsProvider = "njalla";
      # A string, not a path literal: a path would copy the token into the store.
      environmentFile = "/var/lib/acme-secrets/njalla.env";
    };
  };
  systemd.tmpfiles.rules = ["d /var/lib/acme-secrets 0700 root root -"];

  services.caddy = {
    enable = true;
    virtualHosts = lib.mapAttrs' (name: backend:
      lib.nameValuePair "${name}.${domain}" {
        useACMEHost = domain;
        listenAddresses = ["100.64.0.4" "fd7a:115c:a1e0::4"];
        logFormat = null; # no access logs; errors still reach the journal
        extraConfig = "reverse_proxy ${backend}";
      })
    services;
  };
  systemd.services.caddy.serviceConfig = {
    IPAddressDeny = "any";
    IPAddressAllow = ["localhost" "100.64.0.0/10" "fd7a:115c:a1e0::/48"];
  };

  boot.kernel.sysctl = {
    "net.ipv4.ip_nonlocal_bind" = 1;
    "net.ipv6.ip_nonlocal_bind" = 1;
  };
}
