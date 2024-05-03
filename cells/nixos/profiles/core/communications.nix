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
    # cinny-desktop
  ];
  programs.gnupg.agent = {
    enable = true;
    enableSSHSupport = true;
  };
}
