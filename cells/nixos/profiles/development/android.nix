{inputs, common}:
{
  lib,
  # config,
  pkgs,
  ...
}: with lib; {
  # Enable Virtualization for Android Emulator
  boot.kernelModules = ["kvm-amd"];
  virtualisation.libvirtd.enable = true;

  programs.adb.enable = true;
  # users.users.<user>.extraGroups = ["adbusers"]; # Set in users/*/default.nix
  environment.systemPackages = with pkgs; [
    gitRepo
  ];

  # Allow Unfree Android Studio
  # config.allowUnfreePackages = ["android-studio"];
  # config.allowUnfreePredicate = pkg:
  #  builtins.elem (lib.getName pkg) [
  #    "android-studio-stable"
  #  ];
  # config.allowUnfree = true;
}
