{
  inputs,
  common,
}: {
  lib,
  config,
  ...
}: {
  services.syncthing = {
    enable = true;
    openDefaultPorts = true;
    user = "zyansheep";
    dataDir = "/home/zyansheep/"; # Default folder for new synced folders
    configDir = "/home/zyansheep/.config/syncthing"; # Folder for Syncthing's settings and keys
    # Devices and folders are set in the web UI (not overridden here).
  };

  # Don't auto-start syncthing at boot — toggled on demand via the waybar
  # services dropdown.
  systemd.services.syncthing.wantedBy = lib.mkForce [];

  # Allow wheel users to start/stop syncthing without a password,
  # so the waybar services dropdown can toggle it.
  security.polkit.extraConfig = ''
    polkit.addRule(function(action, subject) {
      if (action.id == "org.freedesktop.systemd1.manage-units" &&
          subject.isInGroup("wheel")) {
        var unit = action.lookup("unit");
        var verb = action.lookup("verb");
        var verbs = ["start", "stop", "restart", "reload", "try-restart", "reload-or-restart"];
        if (unit == "syncthing.service" && verbs.indexOf(verb) >= 0) {
          return polkit.Result.YES;
        }
      }
    });
  '';
}
