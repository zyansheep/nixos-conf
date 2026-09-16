{
  lib,
  stdenv,
  fetchFromGitHub,
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
  sources ? null,
}:
stdenv.mkDerivation {
  pname = "nm-sidebar";
  version = "0.1.0";

  src = fetchFromGitHub {
    owner = "Relz";
    repo = "network-manager-sidebar";
    rev = "fa20521c7a127ed8fc3bf8b33478d2e8f9de3901";
    hash = "sha256-HCZN0fxqaNrGtX99AdtK/JXkWzCsKsHaaGa0LW9OKao=";
  };

  patches = [
    ../patches/nm-sidebar/integrated-settings.patch
    ../patches/nm-sidebar/background-input.patch
  ];
  postPatch = ''
    cp ${../patches/nm-sidebar/profile-model.c} src/actions/profile-model.c
    cp ${../patches/nm-sidebar/profile-model.h} src/actions/profile-model.h
    cp ${../patches/nm-sidebar/profile-save.c} src/actions/profile-save.c
    cp ${../patches/nm-sidebar/connection-settings.c} src/sections/connection-settings.c
    substituteInPlace src/sections/connection-settings.c \
      --replace-fail '@defaultWifiMacAddress@' ${lib.escapeShellArg defaultWifiMacAddress} \
      --replace-fail '@usesIwd@' ${if wifiBackend == "iwd" then "1" else "0"}
    cp ${../patches/nm-sidebar/connection-settings.h} src/sections/connection-settings.h
    cat ${../patches/nm-sidebar/settings.css} >> nm-sidebar.css
  '';

  nativeBuildInputs = [ meson ninja pkg-config wrapGAppsHook4 ];
  buildInputs = [ glib gtk4 libadwaita networkmanager gtk4-layer-shell ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    $CC -Wall -Wextra -Werror -I../src \
      ${../patches/nm-sidebar/test-profile-model.c} ../src/actions/profile-model.c \
      $(pkg-config --cflags --libs libnm gio-2.0) -o test-profile-model
    ./test-profile-model
    runHook postCheck
  '';

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
    homepage = "https://github.com/Relz/network-manager-sidebar";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
    mainProgram = "nm-sidebar";
  };
}
