{ inputs, common, }:
{ config, pkgs, lib, ... }:
let
  # Vicinae 0.27.4: label desktop-entry sources and distinguish native/Flatpak
  # window PIDs for focus and quit. Unknown PIDs fall back to launching the app;
  # focus-existing therefore requires a compositor exposing host PIDs (Niri).
  # And give an extension command 5 s, not 1 s, to come up: the first one after
  # a server start waits on the QML views compiling, which on battery took >1 s,
  # and a missed deadline unloads it (an "Extension crashed" screen 5 s later).
  launcher = pkgs.vicinae.overrideAttrs (old: {
    patches = (old.patches or []) ++ [
      ../../../common/patches/vicinae/flatpak-app-identity.patch
      ../../../common/patches/vicinae/extension-handshake-timeout.patch
    ];
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
  imports = [inputs.drainhound.nixosModules.default];
  environment.systemPackages = with pkgs; [
    grim # screenshot functionality
    slurp # rectangle selection for screenshot functionality
    wl-clipboard # wl-copy and wl-paste for copy/paste from stdin / stdout
    cliphist # clipboard manager
    notificationCenter # GTK4 notification panel; background input passes through
    audio-sidebar # Audio devices, levels and per-app routing popup
    display-panel # Brightness, night light and grayscale popup
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
  # iterated on without absolute-pathing every binary. /run/wrappers/bin comes
  # first: the system profile's sudo is the non-setuid store binary ("sudo must
  # be owned by uid 0"), which half-applied the battery menu's combinations.
  # iw is the one tool `drainhound experiment` (the menu's switches) needs that
  # the system profile lacks (it is a per-user package): without it Wi-Fi power
  # saving read as unavailable, so `disable` could not restore it and
  # `goal --apply` reported it failed.
  systemd.user.services.waybar.environment.PATH =
    lib.mkForce "/run/wrappers/bin:${lib.makeBinPath [pkgs.iw]}:/run/current-system/sw/bin";
  # Out-of-store dotfile symlinks keep the same target across rebuilds, so
  # explicitly restart Waybar when its configuration or artwork changes.
  systemd.user.services.waybar.restartTriggers = [
    # `drainhound waybar` runs as Waybar's custom-module children.
    config.services.drainhound.package
    ../../../../dotfiles/.config/waybar/config.jsonc
    ../../../../dotfiles/.config/waybar/style.css
    ../../../../dotfiles/.config/waybar/menu-theme.css
    ../../../../dotfiles/.config/waybar/power_profiles_menu.xml
    ../../../../dotfiles/.config/waybar/gauges
  ];

  # drainhound (github:zyansheep/drainhound): the power log, the battery
  # menu's breakdown and time left, the panel (Mod+Shift+B), experiments, app
  # throttling and freezing, as its own user services. Its data is in
  # ~/.local/state/drainhound; host-specific settings are in the host's config.
  services.drainhound = {
    enable = true;
    settings = {
      # Power off cleanly just before the embedded controller cuts the power.
      battery.poweroff = true;
      # Android-style cached-app freeze for apps that tolerate it: not chat
      # apps (connections drop), not editors running dev servers.
      freeze.apps = ["obsidian" "steam"];
      apps.names."nm-sidebar-gui" = "Wi-Fi panel";
      apps.groups = {
        "Browser" = ["floorp" "firefox" "chromium" "chrome" "brave" "zen"];
        "Builds & EDA" = ["openroad" "yosys" "strands" "rustc" "cargo" "cc1" "c compiler" "c++ compiler" "ld"
                          "make" "nix" "klayout" "verilator" "iverilog" "gcc" "clang" "cmake" "ninja"];
        "Coding tools" = ["t3 code" "vscodium" "vs code" "electron" "claude" "codex" "node" "cursor" "kimi"];
        "Chat & media" = ["vesktop" "signal" "obsidian" "pipewire" "wireplumber" "mpv" "spotify"];
        "Desktop & system" = ["kernel" "niri" "waybar" "notifications" "wi-fi panel" "drainhound" "xwayland"
                              "fcitx5" "vicinae" "systemd" "dbus" "networkmanager" "iwd" "tailscale" "swayidle"
                              "aw-" "xdg-" "swaylock" "awww" "wl-"];
      };
      # The display panel's night light and grayscale, in every record.
      log.extra.panel = "~/.local/state/display-panel/state.json";
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
