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
  environment.systemPackages = [
    pkgs.tor-browser-bundle-bin # The Onion Router
  ];
}
