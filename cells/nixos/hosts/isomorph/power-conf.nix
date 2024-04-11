# Configuration for extending battery life as long as possible
{
  pkgs,
  lib,
  ...
}: {
  boot.kernelPackages = pkgs.linuxKernel.packages.linux_6_8;
  boot.zfs.package = pkgs.zfs_unstable;

  # Enable module that exposes battery charge limit (TODO: WHY DOESN'T THIS WORK???)
  boot.kernelModules = [ "framework-laptop-kmod" ];

  powerManagement = {
    enable = true;
    cpuFreqGovernor = lib.mkDefault "powersave";
    # powertop.enable = true;
  };

  boot.kernelParams = [
    "amdgpu.abmlevel=4" # enable adaptive backlight management (Q: does this actually help with power usage?)
  ];

  
  /* boot.kernelPatches = lib.singleton {
    name = "enable-cpufreq-stat";
    patch = null;
    extraStructuredConfig = with lib.kernel; {
      CONFIG_CPU_FREQ_STAT = yes;
      CONFIG_I2C_DESIGNWARE_PLATFORM = yes;
    };
  }; */
 
}
