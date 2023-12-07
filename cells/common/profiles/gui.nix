{inputs, ...}: {
  self,
  config,
  lib,
  pkgs,
  ...
}: let
  inherit (lib) fileContents;
in {
  fonts.packages = with pkgs; [
    powerline-fonts
    dejavu_fonts
    (
      nerdfonts.override
      {
        fonts = ["Iosevka" "IosevkaTerm"];
      }
    )
  ];
}
