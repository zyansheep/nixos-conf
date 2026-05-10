{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  services.immich = {
    enable = true;
    port = 2283;
    # Bind on the tailscale interface IP so sguru's Caddy can proxy to us
    # over the tailnet. Update if this machine ever re-registers and the
    # IP changes (see `tailscale ip`).
    host = "100.64.0.3";
    mediaLocation = "/var/lib/immich";
    accelerationDevices = null;
    environment.IMMICH_LOG_LEVEL = "warn";
  };

  services.redis.servers.immich.logLevel = "warning";

  users.users.immich.extraGroups = ["video" "render"];

  # Re-expose ~/Pictures outside /home so it's reachable past the immich
  # service's ProtectHome=true sandbox. Read-only by construction.
  systemd.tmpfiles.rules = [
    "d /var/lib/immich-external 0755 root root - -"
    "d /var/lib/immich-external/Pictures 0755 root root - -"
  ];

  fileSystems."/var/lib/immich-external/Pictures" = {
    device = "/home/zyansheep/Pictures";
    fsType = "none";
    options = [
      "bind"
      "ro"
      "x-systemd.requires-mounts-for=/home/zyansheep"
    ];
  };

  # Don't auto-start immich at boot — toggled on demand via the waybar
  # services dropdown. The toggle starts all three units explicitly.
  systemd.services.immich-server.wantedBy = lib.mkForce [];
  systemd.services.immich-machine-learning.wantedBy = lib.mkForce [];
  systemd.services.redis-immich.wantedBy = lib.mkForce [];

  # Allow wheel users to start/stop immich units without a password,
  # so the waybar services dropdown can toggle them.
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          subject.isInGroup("wheel")) {
        var unit = action.lookup("unit");
        var verb = action.lookup("verb");
        var units = [
          "immich-server.service",
          "immich-machine-learning.service",
          "redis-immich.service"
        ];
        var verbs = ["start", "stop", "restart", "reload", "try-restart", "reload-or-restart"];
        if (units.indexOf(unit) >= 0 && verbs.indexOf(verb) >= 0) {
          return polkit.Result.YES;
        }
      }
    });
  '';
}
