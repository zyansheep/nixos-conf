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
  # Other dev tools
  environment.systemPackages = with pkgs; [
    plantuml
    graphviz
  ];
}
