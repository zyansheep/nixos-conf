{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}: {
  services.power-profiles-daemon.enable = false;
  services.tlp = {
    enable = true;
    settings = {
      # CPU_ENERGY_PERF_POLICY_ON_AC = "balance-performance";
      # CPU_SCALING_GOVERNOR_ON_AC = "performance";
      # ENERGY_PERF_POLICY_ON_AC = "performance";

      # CPU_ENERGY_PERF_POLICY_ON_BAT = "power";
      CPU_SCALING_GOVERNOR_ON_BAT = "powersave";
      # ENERGY_PERF_POLICY_ON_BAT = "power";
      RUNTIME_PM_ON_BAT = "auto";

      # The following prevents the battery from charging fully to
      # preserve lifetime. Run `tlp fullcharge` to temporarily force
      # full charge.
      # https://linrunner.de/tlp/faq/battery.html#how-to-choose-good-battery-charge-thresholds
      START_CHARGE_THRESH_BAT0 = 70;
      STOP_CHARGE_THRESH_BAT0 = 75;

      # CPU_MAX_PERF_ON_AC = 100;
      # CPU_MAX_PERF_ON_BAT = 70;
    };
  };
}
