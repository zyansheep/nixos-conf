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
    PATH=${pkgs.lib.makeBinPath [pkgs.coreutils pkgs.powertop turbostat pkgs.util-linux pkgs.gnugrep pkgs.gnused pkgs.pciutils pkgs.systemd pkgs.iw pkgs.jq]}:$PATH
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
      apst <microseconds>    NVMe APST at runtime via pm_qos_latency_tolerance_us (0 = off)
      wifips <on|off>        Wi-Fi power save on the wireless interface
      sleep-safe             restore the boot-time ASPM policy and NVMe APST limit (pre-sleep hook)
      next-boot <default|psr|list>  boot the default or a specialisation entry on the next restart only
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
      apst)
        [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]] || usage
        for f in /sys/class/nvme/nvme*/power/pm_qos_latency_tolerance_us; do echo "$1" > "$f"; echo "$f=$(cat "$f")"; done
        ;;
      wifips)
        [ $# -eq 1 ] && [[ "$1" =~ ^(on|off)$ ]] || usage
        for i in /sys/class/net/*; do
          [ -d "$i/wireless" ] || [ -e "$i/phy80211" ] || continue
          iw dev "$(basename "$i")" set power_save "$1"; iw dev "$(basename "$i")" get power_save
        done
        ;;
      sleep-safe)
        # power-experiment may have relaxed the s2idle crash workarounds while
        # awake; put back whatever the kernel command line asked for.
        policy=$(grep -o 'pcie_aspm.policy=[a-z]*' /proc/cmdline | cut -d= -f2 || true)
        current=$(grep -o '\[[a-z]*\]' /sys/module/pcie_aspm/parameters/policy | tr -d '[]')
        if [ -n "$policy" ] && [ "$policy" != "$current" ]; then echo "$policy" > /sys/module/pcie_aspm/parameters/policy; fi
        latency=$(grep -o 'nvme_core.default_ps_max_latency_us=[0-9]*' /proc/cmdline | cut -d= -f2 || true)
        if [ -n "$latency" ]; then
          for f in /sys/class/nvme/nvme*/power/pm_qos_latency_tolerance_us; do
            [ "$(cat "$f")" = "$latency" ] || echo "$latency" > "$f"
          done
        fi
        ;;
      next-boot)
        [ $# -eq 1 ] && [[ "$1" =~ ^(default|psr|list)$ ]] || usage
        case "$1" in
          list) bootctl list --no-pager ;;
          default) bootctl set-oneshot ""; echo "next boot: default entry" ;;
          *)
            # Newest entry (menu order) built from the matching NixOS specialisation.
            id=$(bootctl list --json=short | jq -r --arg s "$1" \
              '[.[] | select(((.id // "") + " " + (.title // "") + " " + (.version // "")) | test("specialisation[-_ ]?\\(?" + $s; "i"))][0].id // empty')
            [ -n "$id" ] || { echo "no boot entry for specialisation $1" >&2; bootctl list --no-pager >&2; exit 1; }
            bootctl set-oneshot "$id"; echo "next boot: $id"
            ;;
        esac
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
  # Runs as root before every suspend (pre-sleep.service), after any user-level
  # experiment had a chance to change ASPM/APST.
  powerManagement.powerDownCommands = "${powerLab}/bin/power-lab sleep-safe || true";
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
