{ lib, config, pkgs, ... }:
with lib;
{
  environment.systemPackages = with pkgs; [
    signal-desktop
    # cinny-desktop
  ];
  services.pcscd.enable = true;
  programs.gnupg.agent = {
    enable = true;
    enableSSHSupport = true;
  };
}
