# Configuration for extending battery life as long as possible
{
  config,
  pkgs,
  lib,
  ...
}: {
  # amd_pstate requires newest kernel (>=6.5) -> use newest kernel that zfs currently supports
  boot.kernelPackages = pkgs.zfs.latestCompatibleLinuxPackages;
  
  boot.extraModulePackages = with config.boot.kernelPackages; [ framework-laptop-kmod ]; # Load battery limit kmod
  
  powerManagement.cpuFreqGovernor = lib.mkDefault "powersave";
  
  # powerManagement.powertop.enable = true; # Run powertop on boot
  
  boot.kernelParams = [
    "amdgpu.abmlevel=3" # enable adaptive backlight management (Q: does this actually help with power usage?)
  ];

  /* boot.kernelPatches = lib.singleton {
    name = "enable-cpufreq-stat";
    patch = null;
    extraStructuredConfig = with lib.kernel; {
      CONFIG_CPU_FREQ_STAT = yes;
    };
  }; */
}
