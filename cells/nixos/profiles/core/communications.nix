{ inputs, common, }:
{ lib, config, options, pkgs, ... }:
with lib; {
  config = {
    environment.systemPackages = with pkgs;
      [
        signal-desktop
        # cinny-desktop
      ];

    services.mullvad-vpn.enable = true;
    programs.gnupg.agent = {
      enable = true;
      enableSSHSupport = true;
    };
  }
  # Newer nixpkgs split the Mullvad daemon (pkgs.mullvad, now the default for
  # `package`) from the desktop app, which moved behind `gui.enable`. Hosts on
  # 25.11 still ship the combined package and have no `gui` option.
  // optionalAttrs (options.services.mullvad-vpn ? gui) {
    services.mullvad-vpn.gui.enable = true;
  };
}
