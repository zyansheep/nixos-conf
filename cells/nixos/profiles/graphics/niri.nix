{ inputs, common, }:
{ config, pkgs, lib, ... }:
let
  # Vicinae 0.27.4: label desktop-entry sources and distinguish native/Flatpak
  # window PIDs for focus and quit. Unknown PIDs fall back to launching the app;
  # focus-existing therefore requires a compositor exposing host PIDs (Niri).
  launcher = pkgs.vicinae.overrideAttrs (old: {
    patches = (old.patches or []) ++ [ ../../../common/patches/vicinae/flatpak-app-identity.patch ];
    postPatch = (old.postPatch or "") + ''
      cp ${../../../common/patches/vicinae/flatpak-identity.hpp} src/server/src/services/window-manager/flatpak-identity.hpp
      $CXX -std=c++20 -Wall -Wextra -Werror -I${../../../common/patches/vicinae} \
        ${../../../common/patches/vicinae/test-identity.cpp} -o /tmp/test-vicinae-identity
      /tmp/test-vicinae-identity
    '';
  });
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
    display-panel # Brightness, night light and grayscale popup
    waybar-monitor # battery-panel and power-experiment CLIs (collector runs as a service)
    networkSidebar # Wi-Fi popup; uses NetworkManager for connections and storage
    eww # interactive popup widgets (services dropdown)
    zathura # vim pdf viewer
    swayimg # img viewer
    awww # wallpaper daemon (LGFae/awww — successor to swww)
    brightnessctl # brightness control
    launcher # resident application launcher
    swaylock # lockscreen
    swayidle # idle manager
    xwayland-satellite # xwayland support
    nautilus # file picker (for popups)
  ];
  # Keep the launcher loaded; Mod+D only toggles the resident window.
  systemd.user.services.vicinae = {
    description = "Vicinae application launcher";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requires = [ "dbus.socket" ];
    environment.QT_QPA_PLATFORM = "wayland";
    # Desktop entries often use bare commands, including Flatpak's `flatpak run`.
    environment.PATH = lib.mkForce
      "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:%h/.nix-profile/bin:/run/current-system/sw/bin";
    serviceConfig = {
      ExecStart = "${launcher}/bin/vicinae server --replace";
      Restart = "on-failure";
      RestartSec = 3;
    };
  };

  programs.foot.enable = true; # terminal
  programs.waybar.enable = true; # top bar
  # Native battery/profile observers share one icon and open a centered native GTK3 menu.
  # Local patches add group styling/coordinates and bypass click-to-cycle (0.15.0).
  programs.waybar.package = pkgs.waybar.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      cp ${../../../common/patches/waybar-power-menu-stats.hpp} include/util/power_menu_stats.hpp
      cp ${../../../common/patches/waybar-hover-monitor.hpp} include/util/hover_monitor.hpp
    '';
    postCheck = (old.postCheck or "") + ''
      $CXX -std=c++17 -Wall -Wextra -Werror \
        -include ${../../../common/patches/waybar-power-menu-stats.hpp} \
        ${../../../common/patches/test-waybar-power-menu.cpp} $(pkg-config --cflags --libs jsoncpp) -o test-power-menu
      ./test-power-menu
    '';
    patches = (old.patches or []) ++ [
      ../../../common/patches/waybar-power-profile-menu.patch
      ../../../common/patches/waybar-group-menu.patch
      ../../../common/patches/waybar-hover-monitor.patch
      ../../../common/patches/waybar-no-tooltips.patch
      ../../../common/patches/waybar-power-menu-processes.patch
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
    # battery-eta runs as Waybar's custom-module child, so a new build only
    # takes effect when Waybar restarts.
    pkgs.waybar-monitor
    ../../../../dotfiles/.config/waybar/config.jsonc
    ../../../../dotfiles/.config/waybar/style.css
    ../../../../dotfiles/.config/waybar/menu-theme.css
    ../../../../dotfiles/.config/waybar/power_profiles_menu.xml
    ../../../../dotfiles/.config/waybar/gauges
  ];

  systemd.user.services.waybar-monitor = {
    description = "Rolling process, temperature and battery-use statistics for Waybar";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    # `iw` (Wi-Fi power save) and `niri msg` (output modes) for the power log.
    environment.PATH = lib.mkForce
      "/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin";
    serviceConfig = {
      ExecStart = "${pkgs.waybar-monitor}/bin/waybar-monitor";
      Restart = "on-failure";
      # Slow enough that a crash loop never trips the start limit and stops logging.
      RestartSec = 5;
      RuntimeDirectory = "waybar-monitor";
      RuntimeDirectoryMode = "0700";
      # Long-term power log in ~/.local/state/waybar-monitor/power.
      StateDirectory = "waybar-monitor";
      NoNewPrivileges = true;
      # No ProtectSystem/ProtectHome/PrivateTmp: in a user manager they imply a
      # private user namespace, which hides /proc/PID/{fd,fdinfo,io,exe} of the
      # session's apps (per-app GPU time and disk IO for battery attribution).
    };
  };

  # Resident battery panel (history timeline, sleep drain, what-if runtime,
  # experiments), toggled over D-Bus from the battery menu like display-panel.
  systemd.user.services.battery-panel = {
    description = "Resident battery history and experiments panel";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    # Experiment descriptions query iw, niri and powerprofilesctl.
    environment.PATH = lib.mkForce
      "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin";
    serviceConfig = {
      Type = "dbus";
      BusName = "org.zyansheep.BatteryPanel";
      ExecStart = "${pkgs.waybar-monitor}/libexec/waybar-monitor/battery_panel.py";
      Restart = "on-failure";
    };
  };

  # Randomized A/B power experiment, started by `power-experiment start` (the
  # panel's Experiments tab). Root settings go through `sudo -n power-lab`, so
  # no NoNewPrivileges; the original value is restored on stop or crash, and
  # power-lab's pre-sleep hook restores the s2idle crash workarounds.
  systemd.user.services.power-experiment = {
    description = "Randomized A/B battery experiment";
    partOf = [ "graphical-session.target" ];
    environment.PATH = lib.mkForce
      "/run/wrappers/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin";
    serviceConfig = {
      ExecStart = "${pkgs.waybar-monitor}/bin/power-experiment run";
      ExecStopPost = "${pkgs.waybar-monitor}/bin/power-experiment restore";
      Restart = "no";
    };
  };

  # Keep notification widgets and GTK4 loaded before the first panel toggle.
  systemd.user.services.swaync = {
    description = "Resident notification center";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    restartTriggers = [
      ../../../../dotfiles/.config/swaync/config.json
      ../../../../dotfiles/.config/swaync/style.css
      ../../../../dotfiles/.config/waybar/menu-theme.css
    ];
    serviceConfig = {
      Type = "dbus";
      BusName = "org.freedesktop.Notifications";
      ExecStart = "${notificationCenter}/bin/swaync";
      Restart = "on-failure";
    };
  };

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

  # Gamma ramps for the night light; the display panel sets its Temperature over D-Bus.
  systemd.user.services.wl-gammarelay = {
    description = "Wayland gamma/temperature relay";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" ];
    requisite = [ "graphical-session.target" ];
    serviceConfig = {
      Type = "dbus";
      BusName = "rs.wl-gammarelay";
      ExecStart = "${pkgs.wl-gammarelay-rs}/bin/wl-gammarelay-rs run";
      Restart = "on-failure";
    };
  };

  # Resident brightness / night light / grayscale popup. It also owns the
  # grayscale overlay, so it restores saved modes at login.
  systemd.user.services.display-panel = {
    description = "Resident display control popup";
    wantedBy = [ "graphical-session.target" ];
    partOf = [ "graphical-session.target" ];
    after = [ "graphical-session.target" "wl-gammarelay.service" ];
    wants = [ "wl-gammarelay.service" ];
    requisite = [ "graphical-session.target" ];
    serviceConfig = {
      Type = "dbus";
      BusName = "org.zyansheep.DisplayPanel";
      ExecStart = "${pkgs.display-panel}/libexec/display-panel";
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
