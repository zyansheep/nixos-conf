{ lib, config, pkgs, ... }:
with lib;
{
  services.tlp = {
    enable = true;
    settings = {
      # CPU_ENERGY_PERF_POLICY_ON_AC = "balance-performance";
      # CPU_SCALING_GOVERNOR_ON_AC = "performance";
      # ENERGY_PERF_POLICY_ON_AC = "performance";

      # CPU_ENERGY_PERF_POLICY_ON_BAT = "power";
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
      # ENERGY_PERF_POLICY_ON_BAT = "power";
      CPU_BOOST_ON_BAT = 0;
      RUNTIME_PM_ON_BAT = "auto";

      # The following prevents the battery from charging fully to
      # preserve lifetime. Run `tlp fullcharge` to temporarily force
      # full charge.
      # https://linrunner.de/tlp/faq/battery.html#how-to-choose-good-battery-charge-thresholds
      # START_CHARGE_THRESH_BAT0 = 40;
      # STOP_CHARGE_THRESH_BAT0 = 70;

      # CPU_MAX_PERF_ON_AC = 100;
      # CPU_MAX_PERF_ON_BAT = 70;

      # Disable Bluetooth
      DEVICES_TO_DISABLE_ON_STARTUP = "bluetooth";
    };
  };
}
