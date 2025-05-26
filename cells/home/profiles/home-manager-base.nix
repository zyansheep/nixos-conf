_:
{ lib, pkgs, config, ... }: {
  xdg.enable = true;

  home.file = { ".XCompose" = { source = ./_files/XCompose; }; };

  home.pointerCursor = {
    name = "Adwaita";
    package = pkgs.adwaita-icon-theme;
    size = 24;
    x11 = {
      enable = true;
      defaultCursor = "Adwaita";
    };
  };
}
