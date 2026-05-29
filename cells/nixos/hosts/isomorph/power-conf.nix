# Configuration for extending battery life as long as possible
{
  pkgs,
  lib,
  ...
}: {
  # boot.zfs.package = pkgs.zfs_unstable;
  boot = {
    blacklistedKernelModules = ["hid_sensor_hub"];
    # extraModprobeConfig = ''
    #   options snd_hda_intel power_save=1
    # '';
    kernel.sysctl = {
      # enable REISUB: https://www.kernel.org/doc/html/latest/admin-guide/sysrq.html
      "kernel.sysrq" = 1 + 16 + 32 + 64 + 128;
    };
  };
  # Enable module that exposes battery charge limit (TODO: WHY DOESN'T THIS WORK???)
  boot.kernelModules = [ "framework-laptop-kmod" ];

  /* services.udev.extraRules = ''
    SUBSYSTEM=="pci", ATTR{power/control}="auto"
    ACTION=="add", SUBSYSTEM=="usb", TEST=="power/control", ATTR{power/control}="auto"
  ''; */

  powerManagement = {
    enable = true;
    # cpuFreqGovernor = lib.mkDefault "powersave";
    powertop.enable = true;

    # Framework 13 AMD + MT7922 (mt7921e) crashes the SoC during s2idle if the
    # card is left active. Unload before suspend, reload after.
    # https://community.frame.work/t/framework-13-amd-return-from-sleep-causes-reboot/64743
    powerDownCommands = ''
      ${pkgs.kmod}/bin/rmmod mt7921e || true
    '';
    resumeCommands = ''
      ${pkgs.kmod}/bin/modprobe mt7921e
    '';
  };
  # services.tlp.enable = lib.mkForce false;
  # services.power-profiles-daemon.enable = lib.mkForce false;
  # services.auto-cpufreq.enable = lib.mkForce true;
}
