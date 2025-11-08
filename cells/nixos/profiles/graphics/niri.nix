{ inputs, common, }:
{ config, pkgs, lib, ... }: {
  environment.systemPackages = with pkgs; [
    grim # screenshot functionality
    slurp # rectangle selection for screenshot functionality
    wl-clipboard # wl-copy and wl-paste for copy/paste from stdin / stdout
    cliphist # clipboard manager
    mako # notification system developed by swaywm maintainer
    waybar # topbar
    impala # tui wifi manager
    zathura # vim pdf viewer
    swayimg # img viewer
    swaybg # wallpaper
    brightnessctl # brightness control
    rofi # menu
    fuzzel # alternative menu
    swaylock # lockscreen
    mako # notif daemon
    polkit
    swayidle # idle manager
    xwayland-satellite # xwayland support
  ];
  programs.foot.enable = true; # terminal
  programs.waybar.enable = true; # top bar

  services.logind.settings.Login = {
    HandlePowerKey = "ignore"; # ignore power key
    HandleLidSwitch = "suspend"; # lid switch triggers suspend
  };

  # Ensure correct power profile based on plug-in status
  services.udev.extraRules = ''
    # When AC is connected (POWER_SUPPLY_ONLINE=="1"), set performance profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set balanced"
    # When running on battery (POWER_SUPPLY_ONLINE=="0"), set power-saver profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set power-saver"
    # Syncthing
    # SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.systemd}/bin/systemctl start syncthing.service"
    # SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.systemd}/bin/systemctl stop syncthing.service"
  '';

  # Enable wayland-by-default for chromium and electron-based apps
  environment.sessionVariables.NIXOS_OZONE_WL = "1";

  # Enable auto-start niri on login using greetd
  services.greetd = {
    enable = true;
    settings = {
      default_session = {
        command =
          "${pkgs.greetd.tuigreet}/bin/tuigreet --time --cmd niri-session";
        user = "zyansheep";
      };
    };
  };

  # Enable the gnome-keyring secrets vault.
  # Will be exposed through DBus to programs willing to store secrets.
  services.gnome.gnome-keyring.enable = true;

  # Enable Niri window manager
  programs.niri.enable = true;
}
