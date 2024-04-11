_: {
  lib,
  pkgs,
  config,
  ...
}: {
  xdg.enable = true;

  home.file = {
    ".XCompose" = { source = ./_files/XCompose; };
  };
}
