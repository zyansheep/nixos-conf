{
  inputs,
  common,
}: { lib, config, pkgs, ... }:
with lib;
{
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

  # Enable 32 bit libraries for 32-bit games in steam
  # OpenGL options
  hardware.opengl = {
    enable = true;
    driSupport32Bit = true;
    extraPackages32 = with pkgs.pkgsi686Linux; [ libva ];
  };
}
