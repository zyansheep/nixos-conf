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
}
