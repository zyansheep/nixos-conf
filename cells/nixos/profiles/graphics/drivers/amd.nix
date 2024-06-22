{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  hardware.graphics.enable = true;
  hardware.graphics.enable32Bit = true;
}
