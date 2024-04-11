{
  inputs,
  common,
}: {
  lib,
  pkgs,
  ...
}:
with lib; {
  environment.systemPackages = with pkgs; [
    arduino
  ];

  users.users.zyansheep.extraGroups = ["dialout" "tty"];
}
