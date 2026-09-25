{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  wrapGAppsHook4,
  glib,
  gtk4,
  libadwaita,
  networkmanager,
  gtk4-layer-shell,
  networkmanagerapplet,
  adwaita-icon-theme,
  kdePackages,
  defaultWifiMacAddress ? "stable-ssid",
  wifiBackend ? "iwd",
}:
stdenv.mkDerivation {
  pname = "nm-sidebar";
  version = "0.1.0";

  # Git submodule: our fork of Relz/network-manager-sidebar. Needs
  # `inputs.self.submodules = true` in flake.nix.
  src = ../vendor/nm-sidebar;

  mesonFlags = [
    (lib.mesonOption "default_wifi_mac" defaultWifiMacAddress)
    (lib.mesonOption "wifi_backend" wifiBackend)
  ];

  nativeBuildInputs = [ meson ninja pkg-config wrapGAppsHook4 ];
  buildInputs = [ glib gtk4 libadwaita networkmanager gtk4-layer-shell ];
  doCheck = true;

  # The GTK executable lives in libexec; the CLI only sends IPC commands.
  # Keep the editor in the runtime closure without installing nm-applet's
  # XDG autostart entry into the session.
  dontWrapGApps = true;
  postFixup = ''
    wrapProgram "$out/libexec/nm-sidebar/nm-sidebar-gui" \
      --prefix PATH : ${lib.makeBinPath [ networkmanagerapplet ]} \
      --prefix XDG_DATA_DIRS : ${lib.makeSearchPath "share" [ adwaita-icon-theme kdePackages.breeze-icons networkmanagerapplet ]} \
      "''${gappsWrapperArgs[@]}"
  '';

  meta = {
    description = "Interactive NetworkManager sidebar for Wayland";
    homepage = "https://github.com/zyansheep/network-manager-sidebar";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
    mainProgram = "nm-sidebar";
  };
}
