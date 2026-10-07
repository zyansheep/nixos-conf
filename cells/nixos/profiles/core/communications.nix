{ inputs, common, }:
{ lib, config, options, pkgs, ... }:
with lib; {
  config = {
    environment.systemPackages = with pkgs;
      [
        signal-desktop
        # cinny-desktop
      ];

    programs.gnupg.agent = {
      enable = true;
      enableSSHSupport = true;
    };
  };
}
