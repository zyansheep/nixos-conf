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
    zathura # pdf viewer
  ];
  services.logind.powerKey = "ignore"; # ignore power key
  services.logind.lidSwitch = "suspend"; # lid switch triggers suspend

  # Ensure correct power profile based on plug-in status
  services.udev.extraRules = ''
    # When AC is connected (POWER_SUPPLY_ONLINE=="1"), set performance profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set performance"
    # When running on battery (POWER_SUPPLY_ONLINE=="0"), set power-saver profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set power-saver"
    # Syncthing
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.systemd}/bin/systemctl start syncthing.service"
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.systemd}/bin/systemctl stop syncthing.service"
  '';
  # Enable wayland-by-default for chromium and election-based apps
  environment.sessionVariables.NIXOS_OZONE_WL = "1";

  # Syncthing only enable on AC
  systemd.services.syncthing.unitConfig.ConditionACPower = "true";
  # services.udev.extraRules = ''
  #   SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.systemd}/bin/systemctl start syncthing.service"
  #   SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.systemd}/bin/systemctl stop syncthing.service"
  # '';

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
  xdg.portal = {
    enable = true;
    extraPortals = with pkgs;
      [
        xdg-desktop-portal-wlr # wayland roots portal
        # xdg-desktop-portal-gtk # generic gtk portal
      ];
    wlr.enable = true;
  };
}
