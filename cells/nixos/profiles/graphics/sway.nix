{ inputs, common, }:
{ config, pkgs, lib, ... }: {
  environment.systemPackages = with pkgs; [
    grim # screenshot functionality
    slurp # rectangle selection for screenshot functionality
    wl-clipboard # wl-copy and wl-paste for copy/paste from stdin / stdout
    mako # notification system developed by swaywm maintainer
    waybar
    nerd-fonts.symbols-only # symbols for status bar
    font-awesome
    impala # tui wifi manager
    zathula # pdf viewer
  ];

  # services.xserver.displayManager.gdm.enable = true;
  
  # Enable the gnome-keyring secrets vault. 
  # Will be exposed through DBus to programs willing to store secrets.
  services.gnome.gnome-keyring.enable = true;
  # enable Sway window manager
  programs.sway = {
    enable = true;
    wrapperFeatures.gtk = true;
  };
  # programs.waybar.enable = true;

}
