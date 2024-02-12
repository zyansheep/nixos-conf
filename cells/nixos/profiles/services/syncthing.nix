{
  inputs,
  common,
}: { lib, config, ... }:
{
  services.syncthing = {
    enable = true;
    openDefaultPorts = true;
    # overrideDevices = true; # overrides any devices added or deleted through the WebUI
    # overrideFolders = true; # overrides any folders added or deleted through the WebUI
    user = "zyansheep";
    dataDir = "/home/zyansheep/"; # Default folder for new synced folders
    configDir = "/home/zyansheep/.config/syncthing"; # Folder for Syncthing's settings and keys
    # guiAddress = "0.0.0.0:8384";
    /* settings.devices = {
      "dev-admin" = { id = "2XOOBYW-LGKT5UT-YU2PKER-YNQ3JLN-WGDDSW4-JBLY34G-FVX5MZY-H425DQV"; };
      "zyan-server" = { id = "2JQWU24-G5ONURG-24EZ7B3-KIADKXQ-TQZJMQJ-EQBTSTG-CK2LEFG-OC6ZPQE"; };
      "pixelated" = { id = "NE4A7L5-HMX5J2L-ZEJ3WAV-5KIU5R7-FYYYHER-JDFR4UE-LIR3D4U-RKHHYQK"; };
      };
      settings.folders = {
      "Projects" = {
        # Name of folder in Syncthing, also the folder ID
        path = "/home/zyansheep/Projects"; # Which folder to add to Syncthing
        devices = [ "dev-admin" "zyan-server" ]; # Which devices to share the folder with
        versioning = {
          type = "staggered";
          params = {
            cleanInterval = "3600";
            maxAge = "15768000";
          };
        };
      };
      "Knowledge" = {
        path = "/home/zyansheep/Knowledge";
        devices = [ "dev-admin" "zyan-server" "pixelated" ];
        versioning = {
          type = "staggered";
          params = {
            cleanInterval = "3600";
            maxAge = "15768000";
          };
        };
      };
      "devos-conf" = {
        path = "/home/zyansheep/devos-conf";
        devices = [ "dev-admin" "zyan-server" ];
      };
      # Active Sync across all devices
      "sync" = {
        path = "/home/zyansheep/Sync";
        devices = [ "dev-admin" "zyan-server" "pixelated" ];
      };
    }; */
  };
}
