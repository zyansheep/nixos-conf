{
  inputs,
  common,
}: { config, pkgs, ... }:
{
  /* imports = with pkgs; [
    nixos.modules.installer.cd-dvd.installation-cd-base
  ]; */
  # _ = builtins.trace "${pkgs}" pkgs;
  environment.systemPackages = with pkgs; [
    coreutils
    # firefox-wayland # Browser
    # neovim

    # Pipewire
    # plasma-pa
  ];
}
