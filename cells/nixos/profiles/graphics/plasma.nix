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
    qt6.qttools
    okular
    ark
    aha # firmware security window in KDE info center
    wayland-utils # for info center
    clinfo # opencl info for info center
  ];

  services.xserver = {
    enable = true;
    xkb.layout = "us";
    displayManager.sddm.enable = true;
    displayManager.defaultSession = "plasma";
    desktopManager.plasma6.enable = true;
  };

  xdg = {
    portal = {
      enable = true;
      extraPortals = with pkgs; [
        xdg-desktop-portal-wlr
        xdg-desktop-portal-gtk
      ];
    };
  };
}
