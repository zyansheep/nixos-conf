{ lib, stdenvNoCC, python3, gtk4, libadwaita, adwaita-icon-theme, gtk4-layer-shell, gobject-introspection,
  glib, systemd, wrapGAppsHook4 }:
let
  # The collector and experiment runner stay on plain Python; the battery
  # estimator, panel and tests need NumPy/SciPy and pyarrow (the Parquet store),
  # and only the panel needs GTK.
  panelPython = python3.withPackages (p: [ p.pygobject3 p.pycairo p.numpy p.scipy p.pyarrow ]);
  analysisPython = python3.withPackages (p: [ p.numpy p.scipy p.pyarrow ]);
  dir = "$out/libexec/waybar-monitor";
in
stdenvNoCC.mkDerivation {
  pname = "waybar-monitor";
  version = "0.3.0";
  src = ../patches/system-monitor;
  nativeBuildInputs = [ wrapGAppsHook4 gobject-introspection ];
  buildInputs = [ gtk4 libadwaita gtk4-layer-shell ];
  dontBuild = true;
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    ${analysisPython}/bin/python3 -m unittest discover -s . -p 'test_*.py'
    runHook postCheck
  '';
  # Python resolves the bin symlinks, so each entry point finds its modules.
  installPhase = ''
    runHook preInstall
    install -Dm644 -t ${dir} power.py report.py store.py
    install -Dm755 -t ${dir} collector.py experiment.py battery_panel.py eta.py
    sed -i '1s|.*|#!${python3}/bin/python3|' ${dir}/collector.py ${dir}/experiment.py
    sed -i '1s|.*|#!${panelPython}/bin/python3|' ${dir}/battery_panel.py
    sed -i '1s|.*|#!${analysisPython}/bin/python3|' ${dir}/eta.py
    mkdir -p $out/bin
    ln -s ../libexec/waybar-monitor/collector.py $out/bin/waybar-monitor
    ln -s ../libexec/waybar-monitor/experiment.py $out/bin/power-experiment
    ln -s ../libexec/waybar-monitor/eta.py $out/bin/battery-eta
    install -Dm644 ${../../../dotfiles/.config/waybar/menu-theme.css} $out/share/battery-panel/menu-theme.css
    cat > $out/bin/battery-panel <<'SCRIPT'
    #!${stdenvNoCC.shell}
    # Toggle the resident battery panel over D-Bus (starting it if needed);
    # `battery-panel <timeline|sleep|whatif|experiments>` opens on that tab.
    ${systemd}/bin/systemctl --user start battery-panel.service || exit $?
    if [ -n "''${1:-}" ]; then
      action=show; parameter="[<'$1'>]"
    else
      action=toggle; parameter='[]'
    fi
    exec ${glib.bin}/bin/gdbus call --session --dest org.zyansheep.BatteryPanel \
      --object-path /org/zyansheep/BatteryPanel --method org.gtk.Actions.Activate \
      "$action" "$parameter" '{}' >/dev/null
    SCRIPT
    chmod +x $out/bin/battery-panel
    runHook postInstall
  '';
  dontWrapGApps = true;
  preFixup = ''
    gappsWrapperArgs+=(--prefix XDG_DATA_DIRS : ${adwaita-icon-theme}/share)
    gappsWrapperArgs+=(--prefix LD_PRELOAD : ${gtk4-layer-shell}/lib/libgtk4-layer-shell.so)
  '';
  postFixup = ''
    wrapProgram ${dir}/battery_panel.py "''${gappsWrapperArgs[@]}"
  '';
  meta = {
    description = "Waybar process/battery statistics, power log, battery panel and power experiments";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "waybar-monitor";
  };
}
