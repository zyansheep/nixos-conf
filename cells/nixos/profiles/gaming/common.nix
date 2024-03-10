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
    lutris-unwrapped
    prismlauncher

    vulkan-tools
  ];

  # Gamemoderun optimisation command
  programs.gamemode.enable = true;
}
