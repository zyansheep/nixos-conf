{
  self,
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  environment.systemPackages = with pkgs; [
    arduino
  ];

  users.users.zyansheep.extraGroups = ["dialout" "tty"];
}
