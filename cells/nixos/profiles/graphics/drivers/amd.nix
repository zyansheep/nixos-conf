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
  hardware.opengl = {
    enable = true;
    driSupport = true;
  };
}
