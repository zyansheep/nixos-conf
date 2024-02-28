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
    # use flatpak for lutris and steam, they run better
    lutris-unwrapped
    # steam

    prismlauncher
    discord

    vulkan-tools
  ];

  # Gamemoderun optimisation command
  programs.gamemode.enable = true;

  # nixpkgs.config.allowUnfreePackages = [ "steam" "discord" ];
}
