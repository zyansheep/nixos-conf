# dither-calendar's panel under the bar (docs/panel.md): a resident GTK 4 layer-shell popup, toggled over D-Bus.
#
#   pkgs.dither-calendar-panel (vendored from https://calendar.tn.zyancraft.net/panel/, sources in ../patches/dither-calendar-panel)
#
# `dither-calendar-panel` (bin) starts the service if it is not up and toggles the popup; it is what the bar's clock
# runs on a click. The service itself is libexec/dither-calendar-panel (a systemd user unit, Type=dbus,
# BusName=org.zyansheep.CalendarPanel); without such a unit the launcher starts it as a transient one.
{ lib, stdenvNoCC, python3, gtk4, libadwaita, adwaita-icon-theme, gtk4-layer-shell, gobject-introspection, glib, systemd,
  wrapGAppsHook4, xdg-utils }:
let
  python = python3.withPackages (p: [ p.pygobject3 p.pycairo ]);
in
stdenvNoCC.mkDerivation {
  pname = "dither-calendar-panel";
  version = "0.1.0";
  src = ../patches/dither-calendar-panel;
  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 libadwaita gtk4-layer-shell ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    install -Dm755 dither-calendar-panel.py $out/libexec/dither-calendar-panel
    install -Dm644 dither_calendar_model.py $out/libexec/dither_calendar_model.py
    mkdir -p $out/bin
    cat > $out/bin/dither-calendar-panel <<SCRIPT
    #!${stdenvNoCC.shell}
    # Toggle the popup; start the resident panel first if nothing owns its name.
    toggle() {
      ${glib.bin}/bin/gdbus call --session --dest org.zyansheep.CalendarPanel \
        --object-path /org/zyansheep/CalendarPanel --method org.gtk.Actions.Activate toggle '[]' '{}' >/dev/null 2>&1
    }
    owned() {
      ${glib.bin}/bin/gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.NameHasOwner org.zyansheep.CalendarPanel 2>/dev/null | grep -q true
    }
    if ! owned; then
      # The unit a NixOS module installs; else a transient one, so the panel is never a child of the bar.
      ${systemd}/bin/systemctl --user start dither-calendar-panel.service 2>/dev/null ||
        ${systemd}/bin/systemd-run --user --quiet --collect --unit dither-calendar-panel-\$\$ \
          $out/libexec/dither-calendar-panel "\$@" || exit \$?
      for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do owned && break; sleep 0.1; done
    fi
    toggle
    SCRIPT
    chmod +x $out/bin/dither-calendar-panel
    patchShebangs $out/libexec/dither-calendar-panel
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
    gappsWrapperArgs+=(--prefix PATH : ${lib.makeBinPath [ xdg-utils systemd ]})
    gappsWrapperArgs+=(--prefix PYTHONPATH : "${python}/${python3.sitePackages}")
  '';
  postFixup = ''
    wrapProgram "$out/libexec/dither-calendar-panel" "''${gappsWrapperArgs[@]}"
  '';
  propagatedBuildInputs = [ python ];
  meta = {
    description = "Calendar popup under the bar's clock, from dither-calendar";
    license = lib.licenses.agpl3Plus;
    platforms = lib.platforms.linux;
    mainProgram = "dither-calendar-panel";
  };
}
