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
    # latte-dock
    kdePackages.okular
    aha # firmware security window in KDE info center
    wayland-utils # for info center
    clinfo # opencl info for info center
  ];
  services.xserver.enable = true; # can't disable this and enable sddm wayland below as kwallet doesn't unlock
  services.displayManager.sddm.enable = true;
  services.desktopManager.plasma6.enable = true;
  environment.plasma6.excludePackages = [ pkgs.kdePackages.elisa ];

  xdg.portal.extraPortals = with pkgs; [
    kdePackages.xdg-desktop-portal-kde
  ];
}
