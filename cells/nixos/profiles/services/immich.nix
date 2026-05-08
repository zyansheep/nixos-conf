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
    host = "127.0.0.1";
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
