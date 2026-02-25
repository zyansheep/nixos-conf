# Tailscale VPN configuration
_: {
  pkgs,
  config,
  ...
}: {
  services.tailscale = {
    enable = true;
    # Uses pkgs.tailscale from overlay
  };
}
