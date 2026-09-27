{ lib, stdenvNoCC, python3, gtk4, libadwaita, adwaita-icon-theme, gtk4-layer-shell, gobject-introspection, glib, systemd,
  wrapGAppsHook4, brightnessctl, procps, jq }:
let
  python = python3.withPackages (p: [ p.pygobject3 ]);
in
stdenvNoCC.mkDerivation {
  pname = "display-panel";
  version = "0.1.0";
  src = ../patches/display-panel;
  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 libadwaita gtk4-layer-shell ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    install -Dm755 display-panel.py $out/libexec/display-panel
    mkdir -p $out/bin
    cat > $out/bin/display-panel <<'SCRIPT'
    #!${stdenvNoCC.shell}
    # `display-panel status` prints Waybar JSON from the saved state (no Python);
    # otherwise toggle the resident popup over D-Bus.
    if [ "''${1:-}" = status ]; then
      state=''${XDG_STATE_HOME:-$HOME/.local/state}/display-panel/state.json
      [ -r "$state" ] || state=/dev/null
      ${jq}/bin/jq -cn --slurpfile s "$state" '
        ($s[0] // {}) as $st
        | [if $st.night then "night" else empty end, if $st.grayscale then "grayscale" else empty end] as $cls
        | {text: (if $st.night then "" else "" end), class: $cls, alt: ($cls | join("-"))}
      ' 2>/dev/null || echo '{"text":""}'
      exit 0
    fi
    ${systemd}/bin/systemctl --user start display-panel.service || exit $?
    exec ${glib.bin}/bin/gdbus call --session --dest org.zyansheep.DisplayPanel \
      --object-path /org/zyansheep/DisplayPanel --method org.gtk.Actions.Activate \
      toggle '[]' '{}' >/dev/null
    SCRIPT
    chmod +x $out/bin/display-panel
    install -Dm644 ${../../../dotfiles/.config/waybar/menu-theme.css} $out/share/display-panel/menu-theme.css
    patchShebangs $out/libexec/display-panel
    runHook postInstall
  '';
  nativeCheckInputs = [ python ];
  doCheck = true;
  checkPhase = ''
    python3 -m unittest discover -s . -p 'test_*.py'
  '';
  dontWrapGApps = true;
  preFixup = ''
    gappsWrapperArgs+=(--prefix XDG_DATA_DIRS : ${adwaita-icon-theme}/share)
    gappsWrapperArgs+=(--prefix LD_PRELOAD : ${gtk4-layer-shell}/lib/libgtk4-layer-shell.so)
    gappsWrapperArgs+=(--prefix PATH : ${lib.makeBinPath [ brightnessctl procps ]})
    gappsWrapperArgs+=(--prefix PYTHONPATH : "${python}/${python3.sitePackages}")
  '';
  postFixup = ''
    wrapProgram "$out/libexec/display-panel" "''${gappsWrapperArgs[@]}"
  '';
  propagatedBuildInputs = [ python ];
  meta = {
    description = "Wayland popup for backlight, night light and grayscale";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "display-panel";
  };
}
