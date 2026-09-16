{ swaynotificationcenter, sources ? null }:
swaynotificationcenter.overrideAttrs (old: {
  patches = (old.patches or []) ++ [ ../patches/swaync-background-input.patch ];
})
