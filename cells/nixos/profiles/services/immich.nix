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
    # Bind only on loopback. immich never touches the tailnet interface, so it
    # can't crash-loop when tailscale is down/re-registering (the old failure:
    # EADDRNOTAVAIL on a missing tailscale IP), and it stays reachable locally
    # at 127.0.0.1:2283 as an offline backup. The tailnet side is exposed via
    # the immich-tailnet socket proxy below.
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

  # Don't auto-start immich at boot — toggled on demand via the waybar
  # services dropdown. The toggle starts all three units explicitly.
  systemd.services.immich-server.wantedBy = lib.mkForce [];
  systemd.services.immich-machine-learning.wantedBy = lib.mkForce [];
  systemd.services.redis-immich.wantedBy = lib.mkForce [];

  # Expose immich on the tailnet without binding immich itself to a tailscale
  # IP. A socket-activated proxy listens on the tailnet IP only and forwards to
  # immich on loopback. FreeBind lets the socket bind even while the tailnet is
  # down (waybar toggle / re-register), so nothing crash-loops. sguru's Caddy
  # proxies photos.zyancraft.net here. If this host's tailscale IP ever changes,
  # update ListenStream below (see `tailscale ip -4`).
  systemd.sockets.immich-tailnet = {
    description = "immich tailnet listener (100.64.0.2:2283 -> 127.0.0.1:2283)";
    wantedBy = ["sockets.target"];
    socketConfig = {
      ListenStream = "100.64.0.2:2283";
      FreeBind = true;
    };
  };

  systemd.services.immich-tailnet = {
    description = "immich tailnet socket proxy (-> 127.0.0.1:2283)";
    after = ["immich-server.service"];
    serviceConfig = {
      ExecStart = "${config.systemd.package}/lib/systemd/systemd-socket-proxyd --exit-idle-time=5min 127.0.0.1:2283";
      DynamicUser = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
    };
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
