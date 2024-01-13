# Configuration for extending battery life as long as possible
{
  config,
  pkgs,
  lib,
  ...
}: {
  # amd_pstate requires newest kernel (>=6.5) -> use newest kernel that zfs currently supports
  boot.kernelPackages = pkgs.zfs.latestCompatibleLinuxPackages;
  
  boot.extraModulePackages = with config.boot.kernelPackages; [
    (config.boot.kernelPackages.callPackage ./framework-laptop-kmod.nix {})
  ]; # Load battery limit kmod
  boot.kernelModules = [ "framework-laptop-kmod" ];
  # boot.initrd.availableKernelModules = [ "framework-laptop-kmod" ];
  # boot.initrd.kernelModules = [ "framework-laptop-kmod" ];

  powerManagement.cpuFreqGovernor = lib.mkDefault "powersave";
  powerManagement.powertop.enable = true; # Run powertop on boot

  # enable adaptive backlight management
  boot.kernelParams = [ "amdgpu.abmlevel=3" ];
}
