{inputs, common}:
{
  lib,
  pkgs,
  ...
}: with lib; {
  # Enable Virtualization for Android Emulator
  boot.kernelModules = ["kvm-amd"];
  virtualisation.libvirtd.enable = true;

  # systemd 258+ handles uaccess automatically — adbusers group is no longer
  # needed. Just install android-tools to get adb/fastboot.
  environment.systemPackages = with pkgs; [
    git-repo
    android-tools
  ];
}
