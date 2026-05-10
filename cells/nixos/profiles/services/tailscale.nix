{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}: {
  services.tailscale = {
    enable = true;
    # Persist the headscale login-server in tailscaled state so a bare
    # `tailscale up` (e.g. via the waybar toggle on a fresh login) doesn't
    # silently fall back to login.tailscale.com.
    extraSetFlags = ["--login-server=https://headscale.zyancraft.net"];
  };

  # Allow wheel users to start/stop tailscaled without a password,
  # so the waybar services dropdown can connect/disconnect from the tailnet.
  # Stopping tailscaled brings down tailscale0; starting it auto-reconnects
  # using the saved auth state, so this is effectively `tailscale up/down`.
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          subject.isInGroup("wheel")) {
        var unit = action.lookup("unit");
        var verb = action.lookup("verb");
        var verbs = ["start", "stop", "restart", "reload", "try-restart", "reload-or-restart"];
        if (unit == "tailscaled.service" && verbs.indexOf(verb) >= 0) {
          return polkit.Result.YES;
        }
      }
    });
  '';
}
