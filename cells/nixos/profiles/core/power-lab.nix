# `power-lab` — passwordless, fixed-menu root helper for power-consumption
# experiments (powertop reports, amd_pmc/PSR debugfs, and runtime toggles for
# ASPM policy / panel ABM / CPU boost so a change can be A/B-measured against
# the battery discharge rate without a rebuild or reboot).
#
# Same scoped sudo-rs pattern as `rebuild` (core.rebuild): only this immutable
# store-path wrapper is NOPASSWD, and it only accepts the subcommands below.
# `rebuild` is already root-equivalent for zyansheep, so this widens nothing.
_: {
  pkgs,
  config,
  ...
}: let
  turbostat = config.boot.kernelPackages.turbostat;
  powerLab = pkgs.writeShellScriptBin "power-lab" ''
    set -euo pipefail
    PATH=${pkgs.lib.makeBinPath [pkgs.coreutils pkgs.powertop turbostat pkgs.util-linux pkgs.gnugrep pkgs.gnused pkgs.pciutils pkgs.systemd]}:$PATH
    usage() {
      cat >&2 <<'USAGE'
    usage: power-lab <cmd> [args]
      status                 battery draw, SoC power, ASPM policy, ABM level, boost, PSR/s0ix debugfs
      powertop [secs]        powertop CSV report (default 20s) -> prints path
      turbostat [secs]       turbostat summary for N seconds (default 10)
      dmesg [args...]        dmesg
      trigger <subsystem>    udevadm trigger --action=change -s <subsystem>
      lspci [args...]        lspci (root: full capability dump)
      abm <0-4>              amdgpu panel_power_savings (ABM) on the internal panel
      aspm <policy>          pcie_aspm policy: default|performance|powersave|powersupersave
      boost <0|1>            cpufreq boost (global + per-policy)
      sysfs <path> <value>   write <value> to a file under /sys
    USAGE
      exit 2
    }
    cmd=''${1:-}; shift || true
    case "$cmd" in
      status)
        v=$(cat /sys/class/power_supply/BAT1/voltage_now); i=$(cat /sys/class/power_supply/BAT1/current_now)
        echo "battery: $(cat /sys/class/power_supply/BAT1/status) $(awk -v v=$v -v i=$i 'BEGIN{printf "%.2f W", v*i/1e12}') ($(cat /sys/class/power_supply/BAT1/capacity)%)"
        for h in /sys/class/drm/card*/device/hwmon/hwmon*/power1_average; do echo "soc: $(awk '{printf "%.2f W",$1/1e6}' $h)"; done
        echo "aspm: $(cat /sys/module/pcie_aspm/parameters/policy)"
        for p in /sys/class/drm/card*-eDP-1/amdgpu/panel_power_savings; do echo "abm: $(cat $p)"; done
        echo "boost: global=$(cat /sys/devices/system/cpu/cpufreq/boost) cpu0=$(cat /sys/devices/system/cpu/cpu0/cpufreq/boost 2>/dev/null || echo n/a)"
        echo "epp: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference) governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor) platform: $(cat /sys/firmware/acpi/platform_profile)"
        for f in /sys/kernel/debug/dri/*/eDP-1/psr_state /sys/kernel/debug/dri/*/eDP-1/psr_capability; do [ -r "$f" ] && echo "$(basename $f): $(tr '\n' ' ' < $f)"; done
        [ -r /sys/kernel/debug/amd_pmc/s0ix_stats ] && { echo "s0ix_stats:"; sed 's/^/  /' /sys/kernel/debug/amd_pmc/s0ix_stats; }
        [ -r /sys/kernel/debug/dri/1/amdgpu_pm_info ] && grep -E 'GFX Clocks|Clocks|power|Power|GPU Load|MCLK|SCLK' /sys/kernel/debug/dri/1/amdgpu_pm_info | sed 's/^/  pm_info: /' | head -12
        ;;
      powertop)
        secs=''${1:-20}; out=/tmp/powertop-$(date +%s).csv
        powertop --csv="$out" --time="$secs" >/dev/null 2>&1 || true
        chmod 644 "$out"; echo "$out"
        ;;
      turbostat)
        secs=''${1:-10}
        turbostat --quiet --Summary --interval "$secs" --num_iterations 1 2>&1
        ;;
      dmesg) dmesg "$@" ;;
      trigger) [ $# -eq 1 ] || usage; udevadm trigger --action=change -s "$1"; echo triggered ;;
      lspci) lspci "$@" ;;
      abm)
        [ $# -eq 1 ] && [[ "$1" =~ ^[0-4]$ ]] || usage
        for p in /sys/class/drm/card*-eDP-1/amdgpu/panel_power_savings; do echo "$1" > "$p"; echo "$p=$(cat $p)"; done
        ;;
      aspm)
        [ $# -eq 1 ] && [[ "$1" =~ ^(default|performance|powersave|powersupersave)$ ]] || usage
        echo "$1" > /sys/module/pcie_aspm/parameters/policy; cat /sys/module/pcie_aspm/parameters/policy
        ;;
      boost)
        [ $# -eq 1 ] && [[ "$1" =~ ^[01]$ ]] || usage
        echo "$1" > /sys/devices/system/cpu/cpufreq/boost
        for p in /sys/devices/system/cpu/cpu*/cpufreq/boost; do echo "$1" > "$p" 2>/dev/null || true; done
        echo "boost=$(cat /sys/devices/system/cpu/cpufreq/boost)"
        ;;
      sysfs)
        [ $# -eq 2 ] || usage
        case "$1" in /sys/*) ;; *) echo "refusing: $1 not under /sys" >&2; exit 1 ;; esac
        printf '%s' "$2" > "$1"; echo "$1=$(cat "$1")"
        ;;
      *) usage ;;
    esac
  '';
in {
  environment.systemPackages = [powerLab];
  security.sudo-rs.extraRules = [
    {
      users = ["zyansheep"];
      commands = [
        {
          command = "/run/current-system/sw/bin/power-lab";
          options = ["NOPASSWD"];
        }
      ];
    }
  ];
}
