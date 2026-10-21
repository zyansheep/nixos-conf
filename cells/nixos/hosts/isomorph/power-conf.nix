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

  # Panel ABM (AMD Adaptive Backlight Management) on battery.
  #
  # amdgpu exposes it as panel_power_savings (0 = off … 4 = max) on the eDP
  # connector. ABM lowers the backlight and boosts pixel values to compensate,
  # trading a slight contrast shift for ~1 W at full brightness. This is what
  # power-profiles-daemon's amdgpu_panel_power action would do on power-saver,
  # but that action never probes here (busctl lists only trickle_charge), so
  # drive it directly from udev on AC/battery transitions plus the connector's
  # add event at boot (ACAD appears before the DRM connector exists).
  # Measured 2026-09-02 (A/B/A/B, 40 s windows, browser + music workload):
  # abm0 15.35/13.02 W vs abm3 14.07/11.84 W → ~1.2 W.
  # This is ABM's stock value for drainhound's experiments: services.drainhound
  # below says 3 on battery, 0 on AC — change both together.
  services.udev.extraRules = let
    panelAbm = pkgs.writeShellScript "panel-abm" ''
      level=''${1:-}
      if [ -z "$level" ]; then
        level=3
        for ac in /sys/class/power_supply/*; do
          [ "$(cat "$ac/type" 2>/dev/null)" = Mains ] && [ "$(cat "$ac/online" 2>/dev/null)" = 1 ] && level=0
        done
      fi
      for p in /sys/class/drm/card*-eDP-1/amdgpu/panel_power_savings; do
        [ -w "$p" ] && echo "$level" > "$p"
      done
      exit 0
    '';
  in ''
    SUBSYSTEM=="power_supply", ATTR{type}=="Mains", ATTR{online}=="1", RUN+="${panelAbm} 0"
    SUBSYSTEM=="power_supply", ATTR{type}=="Mains", ATTR{online}=="0", RUN+="${panelAbm} 3"
    ACTION=="add", SUBSYSTEM=="drm", KERNEL=="card*-eDP-1", RUN+="${panelAbm}"
  '';

  # Boot-level A/B for panel self-refresh. nixos-hardware's Framework 13 AMD
  # module disables PSR (amdgpu.dcdebugmask=0x10) for hangs reported in 2024
  # (drm/amd#3647); PSR lets the display engine idle on a static screen. This
  # entry re-enables it: the later module parameter wins. Pick it for one boot
  # with `drainhound experiment next-boot psr` (or the boot menu); the power log
  # records the running dcdebugmask and the battery panel compares boots.
  specialisation.psr.configuration = {
    boot.kernelParams = lib.mkAfter [ "amdgpu.dcdebugmask=0x0" ];
  };

  # drainhound on this laptop (the service itself is in the niri profile):
  # its root helper may run without a password for experiments' root-only
  # settings, ABM's stock is the udev rule above, and the psr specialisation
  # is a boot-level experiment arm.
  services.drainhound = {
    helper.users = [ "zyansheep" ];
    settings.stock.abm = { battery = 3; ac = 0; };
    settings.experiments.boot_arms.psr = "psr";
  };

  powerManagement = {
    enable = true;
    # cpuFreqGovernor = lib.mkDefault "powersave";
    powertop.enable = true;

    # Framework 13 AMD + MT7922 (mt7921e) crashes the SoC during s2idle if the
    # card is left active. Unload before suspend, reload after.
    # https://community.frame.work/t/framework-13-amd-return-from-sleep-causes-reboot/64743
    # The resume commands run as one `set -e` script (sleep-actions.service's
    # stop), in no fixed order: a failed reload must not skip the rest (power-lab
    # sleep-restore, which ends power-lab's suspend lock-out).
    powerDownCommands = ''
      ${pkgs.kmod}/bin/rmmod mt7921e || true
    '';
    resumeCommands = ''
      ${pkgs.kmod}/bin/modprobe mt7921e || echo "power-conf: modprobe mt7921e failed after resume" >&2
    '';
  };
  # services.tlp.enable = lib.mkForce false;
  # services.power-profiles-daemon.enable = lib.mkForce false;
  # services.auto-cpufreq.enable = lib.mkForce true;
}
