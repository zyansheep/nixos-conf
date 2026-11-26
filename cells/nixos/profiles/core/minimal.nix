# Default Minimal Computer Configuration
{ inputs, common, }:
{ lib, config, pkgs, ... }:
with lib; {
  environment.systemPackages = with pkgs; [
    vim
    git
    git-lfs
  ];

  # Send notifications when program is killed due to OOM
  services.earlyoom.enable = lib.mkDefault true;
  services.earlyoom.enableNotifications = true;

  security.sudo.enable = false;
  security.sudo-rs.enable = true;

  environment.shellAliases = { neofetch = "fastfetch"; };

  # machined is socket-activated, so a switch that changes it stops its socket and its service and then starts the
  # socket; the first name lookup in between (nsswitch's `mymachines`) starts the service over D-Bus, the socket then
  # refuses to start, and the switch fails and rolls back (systemd 262). Restart the service in place instead.
  systemd.services.systemd-machined.stopIfChanged = false;
}
