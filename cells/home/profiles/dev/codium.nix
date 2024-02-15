{
  inputs,
  cell,
}: {
  pkgs,
  lib,
  ...
}: let
  exts = inputs.nix-vscode-extensions.extensions;
  open-vsx = exts.open-vsx;
in {
  programs.vscode = {
    enable = true;
    package = inputs.cells.common.overrides.vscodium;
    # TODO split extensions based on active modules
    extensions = with open-vsx; [zokugun.sync-settings]; # use sync-settings w/ syncthing to sync settings across computers
  };
}
