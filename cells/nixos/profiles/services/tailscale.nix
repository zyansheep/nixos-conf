{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}: {
  services.tailscale.enable = true;

  # NOTE: the headscale login-server is NOT set declaratively here.
  # `--login-server` is only valid for `tailscale up`/`tailscale login`, not
  # `tailscale set` — putting it in extraSetFlags makes tailscaled-set.service
  # fail (exit 2 / INVALIDARGUMENT) and never applies it. Instead, log in once
  # with `tailscale up --login-server=https://headscale.zyancraft.net`; the
  # ControlURL is then persisted in /var/lib/tailscale (impermanence-protected,
  # see hosts/isomorph/default.nix), so a bare `tailscale up` via the waybar
  # toggle reuses headscale instead of falling back to login.tailscale.com.
  # (For fully declarative re-auth, add services.tailscale.authKeyFile pointing
  #  at a headscale preauthkey + extraUpFlags = ["--login-server=..."].)

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
