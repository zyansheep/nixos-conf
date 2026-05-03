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
}
