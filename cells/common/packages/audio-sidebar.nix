{ lib, stdenvNoCC, python3, gtk4, gtk4-layer-shell, gobject-introspection, glib, systemd,
  wrapGAppsHook4, pulseaudio, helvum, sources ? null }:
stdenvNoCC.mkDerivation {
  pname = "audio-sidebar";
  version = "0.1.0";
  src = ../patches/audio-sidebar;
  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 gtk4-layer-shell ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    install -Dm755 audio-sidebar.py $out/libexec/audio-sidebar
    mkdir -p $out/bin
    cat > $out/bin/audio-sidebar <<'SCRIPT'
    #!${stdenvNoCC.shell}
    # Starting an already-running service is cheap; no Python on the click path.
    ${systemd}/bin/systemctl --user start audio-sidebar.service || exit $?
    exec ${glib.bin}/bin/gdbus call --session --dest org.zyansheep.AudioSidebar \
      --object-path /org/zyansheep/AudioSidebar --method org.gtk.Actions.Activate \
      toggle '[]' '{}' >/dev/null
    SCRIPT
    chmod +x $out/bin/audio-sidebar
    install -Dm644 ${../../../dotfiles/.config/waybar/menu-theme.css} $out/share/audio-sidebar/menu-theme.css
    patchShebangs $out/libexec/audio-sidebar
    runHook postInstall
  '';
  nativeCheckInputs = [ (python3.withPackages (p: [ p.pygobject3 ])) ];
  doCheck = true;
  checkPhase = ''
    python3 -m unittest discover -s . -p 'test_*.py'
  '';
  dontWrapGApps = true;
  preFixup = ''
    gappsWrapperArgs+=(--prefix LD_PRELOAD : ${gtk4-layer-shell}/lib/libgtk4-layer-shell.so)
    gappsWrapperArgs+=(--prefix PATH : ${lib.makeBinPath [ pulseaudio helvum ]})
    gappsWrapperArgs+=(--prefix PYTHONPATH : "${python3.withPackages (p: [ p.pygobject3 ])}/${python3.sitePackages}")
  '';
  postFixup = ''
    wrapProgram "$out/libexec/audio-sidebar" "''${gappsWrapperArgs[@]}"
  '';
  # Supply the interpreter to patchShebangs as well as the GI Python module.
  propagatedBuildInputs = [ (python3.withPackages (p: [ p.pygobject3 ])) ];
  meta = {
    description = "Wayland audio popup with devices, volume and application routing";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "audio-sidebar";
  };
}
