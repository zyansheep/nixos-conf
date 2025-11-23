{ inputs, common, }:
{ lib, config, pkgs, ... }:
with lib; {
  environment.systemPackages = with pkgs;
    [
      signal-desktop
      # cinny-desktop
    ];

  services.mullvad-vpn.enable = true;
  services.mullvad-vpn.package = pkgs.mullvad-vpn;
  programs.gnupg.agent = {
    enable = true;
    enableSSHSupport = true;
  };
}
