{ swaynotificationcenter, adwaita-icon-theme, sources ? null }:
swaynotificationcenter.overrideAttrs (old: {
  preFixup = (old.preFixup or "") + ''
    gappsWrapperArgs+=(--prefix XDG_DATA_DIRS : ${adwaita-icon-theme}/share)
  '';
  patches = (old.patches or []) ++ [ ../patches/swaync-background-input.patch ../patches/swaync-resident-panel.patch ];
})
