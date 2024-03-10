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
  environment.systemPackages = with pkgs; [
    signal-desktop
    discord
    # cinny-desktop
  ];
  programs.gnupg.agent = {
    enable = true;
    enableSSHSupport = true;
  };
}
