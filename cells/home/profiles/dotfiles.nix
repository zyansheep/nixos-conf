_: {
  config,
  lib,
  ...
}: let
  repoRoot = "${config.home.homeDirectory}/nixos-conf";
  dotfilesDir = "${repoRoot}/dotfiles/.config";
  mkSymlink = config.lib.file.mkOutOfStoreSymlink;

  # Auto-discover all files under a subdirectory and create xdg.configFile entries
  # nixPath: Nix path for builtins.readDir (must be a path literal for purity)
  # name: the .config subdirectory name (e.g. "waybar")
  mkConfigSymlinks = name: nixPath:
    let
      readDirRec = relPath: dirPath:
        lib.concatLists (lib.mapAttrsToList (entry: type:
          let
            relPath' = if relPath == "" then entry else "${relPath}/${entry}";
          in
          if type == "directory"
          then readDirRec relPath' (dirPath + "/${entry}")
          else [
            {
              name = "${name}/${relPath'}";
              value.source = mkSymlink "${dotfilesDir}/${name}/${relPath'}";
            }
          ]
        ) (builtins.readDir dirPath));
    in
    builtins.listToAttrs (readDirRec "" nixPath);

  dotfilesPath = ../../../dotfiles/.config;
in {
  xdg.configFile =
    mkConfigSymlinks "niri" (dotfilesPath + "/niri")
    // mkConfigSymlinks "waybar" (dotfilesPath + "/waybar")
    // mkConfigSymlinks "swaync" (dotfilesPath + "/swaync")
    // mkConfigSymlinks "helix" (dotfilesPath + "/helix");
}
