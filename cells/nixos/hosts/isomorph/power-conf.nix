# Configuration for extending battery life as long as possible
{
  config,
  pkgs,
  lib,
  ...
}: {
  boot.kernelPackages = pkgs.zfsUnstable.latestCompatibleLinuxPackages;

  # Enable module that exposes battery charge limit (TODO: WHY DOESN'T THIS WORK???)
  boot.extraModulePackages = with pkgs.zfsUnstable.latestCompatibleLinuxPackages; [ framework-laptop-kmod ];

  powerManagement = {
    enable = true;
    cpuFreqGovernor = lib.mkDefault "powersave";
    # powertop.enable = true;
  };


  boot.kernelParams = [
    # "amdgpu.abmlevel=3" # enable adaptive backlight management (Q: does this actually help with power usage?)
  ];

  /* boot.kernelPatches = lib.singleton {
    name = "enable-cpufreq-stat";
    patch = null;
    extraStructuredConfig = with lib.kernel; {
      CONFIG_CPU_FREQ_STAT = yes;
    };
  }; */
}
