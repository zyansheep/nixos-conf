# Default Minimal Computer Configuration
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
  environment.systemPackages = with pkgs; [
    vim
    git
    git-lfs
  ];

  # Enable Flakes and the Nix Command
  # Automatically clear old packages
  /* nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  }; */

  # Send notifications when program is killed due to OOM
  services.earlyoom.enable = lib.mkDefault true;
  services.earlyoom.enableNotifications = true;

  # Enable doas instead of sudo
  security.sudo.enable = false;
  security.doas.enable = true;
  security.doas.extraRules = [
    {
      groups = ["users"];
      keepEnv = true;
      persist = true;
    }
  ];

  environment.shellAliases = {
    sudo = "doas";
  };
}
