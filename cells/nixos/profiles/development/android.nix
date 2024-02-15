{
  lib,
  config,
  pkgs,
  ...
}: {
  # Enable Virtualization for Android Emulator
  boot.kernelModules = ["kvm-amd"];
  virtualisation.libvirtd.enable = true;

  programs.adb.enable = true;
  # users.users.<user>.extraGroups = ["adbusers"]; # Set in users/*/default.nix
  environment.systemPackages = with pkgs; [
    gitRepo
  ];

  # Allow Unfree Android Studio
  nixpkgs.config.allowUnfreePackages = ["android-studio"];
  nixpkgs.config.allowUnfreePredicate = pkg:
    builtins.elem (lib.getName pkg) [
      "android-studio-stable"
    ];
  nixpkgs.config.allowUnfree = true;
}
