{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  inputs,
  ...
}:
with lib; {
  environment.systemPackages = [
    pkgs.osu-lazer
  ];

  /*
  nixpkgs.config.allowUnfreePackages = [ "osu-lazer" ];
  */
}
