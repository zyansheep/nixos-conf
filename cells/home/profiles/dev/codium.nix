_: {
  pkgs,
  lib,
  ...
}: {
  programs.vscode = {
    enable = true;
    package = pkgs.vscodium; # Uses vscodium from overlay (latest)
    # TODO split extensions based on active modules
    /* profiles.default.extensions = with open-vsx;
       [
         zokugun.sync-settings
         # (just use docker)
         # pkgs.vscode-extensions.jebbs.plantuml # built-in one doesn't integrate with nix

       ]; # use sync-settings w/ syncthing to sync settings across computers
    */
  };
}
