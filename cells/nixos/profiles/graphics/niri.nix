{ inputs, common, }:
{ config, pkgs, lib, ... }:
let
  notificationCenter = pkgs.notification-sidebar;
  networkSidebar = pkgs.nm-sidebar.override {
    defaultWifiMacAddress = config.networking.networkmanager.wifi.macAddress;
    wifiBackend = config.networking.networkmanager.wifi.backend;
  };
in {
  environment.systemPackages = with pkgs; [
    grim # screenshot functionality
    slurp # rectangle selection for screenshot functionality
    wl-clipboard # wl-copy and wl-paste for copy/paste from stdin / stdout
    cliphist # clipboard manager
    notificationCenter # GTK4 notification panel; background input passes through
    audio-sidebar # Audio devices, levels and per-app routing popup
    networkSidebar # Wi-Fi popup; uses NetworkManager for connections and storage
    eww # interactive popup widgets (services dropdown)
    zathura # vim pdf viewer
    swayimg # img viewer
    swaybg # wallpaper (legacy / fallback)
    awww # wallpaper daemon (LGFae/awww — successor to swww)
    brightnessctl # brightness control
    rofi # menu
    fuzzel # alternative menu
    swaylock # lockscreen
    polkit
    swayidle # idle manager
    xwayland-satellite # xwayland support
    nautilus # file picker (for popups)
  ];
  programs.foot.enable = true; # terminal
  programs.waybar.enable = true; # top bar
  # Native battery/profile observers share one icon and open a centered native GTK3 menu.
  # Local patches add group styling/coordinates and bypass click-to-cycle (0.15.0).
  programs.waybar.package = pkgs.waybar.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      cp ${../../../common/patches/waybar-power-menu-stats.hpp} include/util/power_menu_stats.hpp
    '';
    postCheck = (old.postCheck or "") + ''
      $CXX -std=c++17 -Wall -Wextra -Werror \
        -include ${../../../common/patches/waybar-power-menu-stats.hpp} \
        ${../../../common/patches/test-waybar-power-menu.cpp} -o test-power-menu
      ./test-power-menu
    '';
    patches = (old.patches or []) ++ [
      ../../../common/patches/waybar-power-profile-menu.patch
      ../../../common/patches/waybar-group-menu.patch
      ../../../common/patches/waybar-no-tooltips.patch
    ];
  });

  # Installing swaylock alone does not create its PAM authentication service.
  # Use password authentication without waiting for the fingerprint reader.
  security.pam.services.swaylock = {
    fprintAuth = false;
  };

  # Handle loginctl lock-session and lock before suspend/lid-close. With -w
  # and swaylock -f, swayidle holds the sleep inhibitor until locking finishes.
  systemd.user.services.swayidle = {
    description = "Lock the Niri session on request and before sleep";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.swayidle}/bin/swayidle -w lock '${pkgs.swaylock}/bin/swaylock -f' before-sleep '${pkgs.swaylock}/bin/swaylock -f'";
      Restart = "on-failure";
    };
  };

  # Make system-wide binaries available to waybar's exec scripts (jq, bash,
  # systemctl, notify-send, etc.). The default unit PATH is intentionally
  # minimal; this widens it to the system profile so custom modules can be
  # iterated on without absolute-pathing every binary.
  systemd.user.services.waybar.environment.PATH =
    lib.mkForce "/run/current-system/sw/bin";
  # Out-of-store dotfile symlinks keep the same target across rebuilds, so
  # explicitly restart Waybar when its configuration or artwork changes.
  systemd.user.services.waybar.restartTriggers = [
    ../../../../dotfiles/.config/waybar/config.jsonc
    ../../../../dotfiles/.config/waybar/style.css
    ../../../../dotfiles/.config/waybar/menu-theme.css
    ../../../../dotfiles/.config/waybar/power_profiles_menu.xml
    ../../../../dotfiles/.config/waybar/gauges
  ];

  # Preload GTK and build the audio widgets once per graphical session.
  systemd.user.services.audio-sidebar = {
    description = "Resident audio control popup";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    serviceConfig = {
      Type = "dbus";
      BusName = "org.zyansheep.AudioSidebar";
      ExecStart = "${pkgs.audio-sidebar}/libexec/audio-sidebar";
      Restart = "on-failure";
    };
  };

  # Keep the long-lived sidebar attached to the session and replace it on
  # upgrades; otherwise a new CLI keeps talking to an old background GUI.
  systemd.user.services.nm-sidebar = {
    description = "Interactive network sidebar";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    serviceConfig = {
      ExecStartPre = "-${networkSidebar}/bin/nm-sidebar --quit";
      ExecStart = "${networkSidebar}/libexec/nm-sidebar/nm-sidebar-gui background";
      Restart = "on-failure";
    };
  };

  services.logind.settings.Login = {
    HandlePowerKey = "ignore"; # ignore power key
    HandleLidSwitch = "suspend"; # lid switch triggers suspend
  };

  # Ensure correct power profile based on plug-in status
  # Only the mains adapter: the ucsi-source-psy-* USB-C power supplies also emit
  # POWER_SUPPLY_ONLINE=0 on every port event, which would flip the profile to
  # power-saver while plugged in.
  services.udev.extraRules = ''
    # When AC is connected (POWER_SUPPLY_ONLINE=="1"), set performance profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_TYPE}=="Mains", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set balanced"
    # When running on battery (POWER_SUPPLY_ONLINE=="0"), set power-saver profile.
    SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_TYPE}=="Mains", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.power-profiles-daemon}/bin/powerprofilesctl set power-saver"
    # Syncthing
    # SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_TYPE}=="Mains", ENV{POWER_SUPPLY_ONLINE}=="1", RUN+="${pkgs.systemd}/bin/systemctl start syncthing.service"
    # SUBSYSTEM=="power_supply", ENV{POWER_SUPPLY_TYPE}=="Mains", ENV{POWER_SUPPLY_ONLINE}=="0", RUN+="${pkgs.systemd}/bin/systemctl stop syncthing.service"
  '';

  # Fix file picker
  environment.sessionVariables = {
    XDG_CURRENT_DESKTOP = "niri";
    XDG_SESSION_TYPE = "wayland";
    XDG_SESSION_DESKTOP = "niri";
  };

  # Enable wayland-by-default for chromium and electron-based apps
  environment.sessionVariables.NIXOS_OZONE_WL = "1";

  # Enable auto-start niri on login using greetd; launches niri via uwsm so
  # spawned apps land in their own systemd scopes (OOM-isolated from the compositor).
  services.greetd = {
    enable = true;
    settings = {
      default_session = {
        command =
          "${pkgs.tuigreet}/bin/tuigreet --time --cmd '${pkgs.uwsm}/bin/uwsm start -F -- /run/current-system/sw/bin/niri --session'";
        user = "zyansheep";
      };
    };
  };

  # Enable the gnome-keyring secrets vault.
  # Will be exposed through DBus to programs willing to store secrets.
  services.gnome.gnome-keyring.enable = true;

  # Enable Niri window manager
  programs.niri.enable = true;

  # Manage niri as a uwsm-wrapped Wayland session.
  programs.uwsm.enable = true;
  programs.uwsm.waylandCompositors.niri = {
    prettyName = "Niri";
    comment = "A scrollable-tiling Wayland compositor (UWSM)";
    binPath = "/run/current-system/sw/bin/niri";
    extraArgs = [ "--session" ];
  };
}
