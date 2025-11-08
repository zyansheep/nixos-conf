{ inputs, common, }:
{ config, pkgs, lib, ... }: {
  environment.systemPackages = with pkgs; [
    grim # screenshot functionality
    slurp # rectangle selection for screenshot functionality
    wl-clipboard # wl-copy and wl-paste for copy/paste from stdin / stdout
    cliphist # clipboard manager
    rofi # run menu + clipboard selection
    mako # notification system developed by swaywm maintainer
    waybar # topbar
    impala # tui wifi manager
    zathura # vim pdf viewer
    swayimg
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
  # systemd.services.syncthing.unitConfig.ConditionACPower = "true";

  # enable auto-start sway on login
  services.greetd = {
    enable = true;
    settings = {
      default_session = {
        command = "${pkgs.greetd.tuigreet}/bin/tuigreet --time --cmd sway";
        user = "zyansheep";
      };
    };
  };

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
    extraPortals = with pkgs; [
      xdg-desktop-portal-wlr # wayland roots portal
      xdg-desktop-portal-gtk # generic gtk portal for fallback
      xdg-desktop-portal-termfilechooser # allow open folders in yazi file viewer
    ];
    wlr.enable = true;
  };
  # Fix flatpak links not opening browser
  systemd.user.extraConfig = ''
    DefaultEnvironment="PATH=/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/nix/var/nix/profiles/default/bin:/run/current-system/sw/bin"
  '';
}
