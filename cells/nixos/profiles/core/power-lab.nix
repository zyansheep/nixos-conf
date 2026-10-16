# `power-lab` — passwordless, fixed-menu root helper for power-consumption
# experiments (powertop reports, amd_pmc/PSR debugfs, and runtime toggles for
# ASPM policy / panel ABM / CPU boost so a change can be A/B-measured against
# the battery discharge rate without a rebuild or reboot).
#
# Same scoped sudo-rs pattern as `rebuild` (core.rebuild): only this immutable
# store-path wrapper is NOPASSWD, and it only accepts the subcommands below.
# `rebuild` is already root-equivalent for zyansheep, so this widens nothing;
# still, every subcommand writes only the files it is about (no generic sysfs
# writer, no dmesg --clear or console-level changes, no file-reading flags).
#
# Changes take /run/power-lab.lock, so the runner, the panel and the suspend
# hooks never interleave (set_aspm wakes every PCI function, then puts each
# back). The lock and /run/power-lab are root-only (umask 077): anyone who can
# open a file can hold a flock on it. Between sleep-safe (pre-sleep) and
# sleep-restore (post-resume) the `sleeping` flag makes changes refuse (exit
# 75), so nothing undoes the s2idle crash workarounds on the way into suspend.
# After resume the workarounds stay: power-experiment sets its risky settings
# (ASPM, APST, GPU DPM) again after its post-resume wait and within its hold.
#
# ./power-lab-test.sh checks the script against a fake /sys, /run and
# /proc/cmdline at build time (system.checks), so a rebuild fails if it breaks.
_: {
  pkgs,
  config,
  ...
}: let
  turbostat = config.boot.kernelPackages.turbostat;
  powerLab = pkgs.writeShellScriptBin "power-lab" ''
    set -euo pipefail
    shopt -s nullglob
    umask 077 # the lock and /run/power-lab: root's alone
    PATH=${pkgs.lib.makeBinPath [pkgs.coreutils pkgs.powertop turbostat pkgs.util-linux pkgs.gnugrep pkgs.gnused pkgs.gawk pkgs.pciutils pkgs.systemd pkgs.iw pkgs.jq]}:$PATH
    # Roots, one per line, so tests can point them at a fake tree.
    SYS=/sys
    RUN=/run
    CMDLINE=/proc/cmdline
    LOCK=$RUN/power-lab.lock
    SLEEPING=$RUN/power-lab/sleeping
    ASPM=$SYS/module/pcie_aspm/parameters/policy
    BUSY=75 # EX_TEMPFAIL: suspending, or another change holds the lock; power-experiment waits.
    CHANGE_WAIT=10 # s a change waits for another's lock (power-experiment gives a call 30 s)
    SLEEP_HOOKS=sleep-actions.service # NixOS's unit around suspend: sleep-safe is its start, sleep-restore its stop
    usage() {
      cat >&2 <<'USAGE'
    usage: power-lab <cmd> [args]
      status                 battery draw, SoC power, ASPM policy, ABM level, boost, PSR/s0ix debugfs
      powertop [secs]        powertop CSV report (default 20s) -> prints path
      turbostat [secs]       turbostat summary for N seconds (default 10)
      dmesg [flags...]       dmesg, read-only flags only (-T -x -k -l LEVELS -w --since ...)
      trigger power_supply   re-run the power_supply udev rules (ABM, profile) and wait for them
      lspci [flags...]       lspci (root: capabilities), read-only flags only (-v.. -nn -k -t -s BDF -d ID)
      abm <0-4>              amdgpu panel_power_savings (ABM) on the internal panel
      aspm <policy>          pcie_aspm policy: default|performance|powersave|powersupersave
      boost <0|1>            cpufreq boost per CPU (the global switch stays on)
      apst <microseconds>    NVMe APST at runtime via pm_qos_latency_tolerance_us (0 = off)
      wifips <on|off>        Wi-Fi power save on the wireless interface
      gpudpm <auto|low>      amdgpu DPM level: low pins the GPU, memory and fabric clocks at their lowest
      cpucap <MHz|none>      CPU frequency cap (scaling_max_freq on every policy)
      xhci-reset [BDF]       unbind and rebind a dead xHCI controller (default 0000:c1:00.3)
      sleep-safe             restore the boot-time ASPM policy, NVMe APST limit and GPU DPM auto (pre-sleep hook only)
      sleep-restore          end the suspend lock-out; the workarounds stay (post-resume hook only)
      next-boot <default|psr|list>  boot the default or a specialisation entry on the next restart only
    Changes exit 75 while suspending (sleep-safe ran, sleep-restore not yet) or while another holds the lock.
    USAGE
      exit 2
    }
    log() { # message [priority]: the journal (logger) and stderr
      logger -t power-lab -p "user.''${2:-notice}" -- "$1" 2>/dev/null || true
      echo "power-lab: $1" >&2
    }
    # set_aspm's saved power/control values: put back on any exit, so a failure
    # half-way never leaves every PCI function pinned awake until reboot.
    controls=""
    put_back_controls() {
      local c value
      [ -n "$controls" ] && [ -f "$controls" ] || return 0
      while read -r c value; do
        echo "$value" > "$c" 2>/dev/null || log "could not put back $c=$value" warning
      done < "$controls"
      rm -f "$controls"
      controls=""
    }
    cmd=''${1:-}
    on_exit() {
      local rc=$?
      put_back_controls
      case "$rc" in
        0|2|"$BUSY") ;;
        *) log "$cmd failed (exit $rc)" err ;;
      esac
      exit "$rc"
    }
    trap on_exit EXIT
    lock() { # seconds to wait for the other change
      exec 9> "$LOCK"
      flock -w "$1" 9
    }
    # A change: wait for the lock, and refuse while suspending. The flag holds for
    # as long as the sleep hooks' unit is up: from sleep-safe (its start) through
    # the suspend and resume until sleep-restore (its stop) has run. Once the unit
    # is down without sleep-restore having cleared it (an aborted suspend, a
    # failed hook) the flag is stale. Not the suspend count: that rises on resume,
    # before sleep-restore runs.
    begin_change() {
      lock "$CHANGE_WAIT" || { log "busy: another change holds $LOCK" warning; exit "$BUSY"; }
      [ -e "$SLEEPING" ] || return 0
      case "$(systemctl show -P ActiveState "$SLEEP_HOOKS" 2>/dev/null || true)" in
        activating|active|deactivating|reloading)
          echo "power-lab: suspending (sleep-safe ran, sleep-restore not yet); try again after resume" >&2
          exit "$BUSY" ;;
      esac
      log "clearing a stale sleeping flag ($SLEEP_HOOKS is down: sleep-restore did not run)" warning
      rm -f "$SLEEPING"
    }
    hooks_only() { # sleep-safe/-restore belong to the suspend hooks, not to sudo
      [ -z "''${SUDO_UID:-}" ] || { echo "power-lab: $cmd runs from the suspend hooks only" >&2; exit 2; }
    }
    aspm_now() { grep -o '\[[a-z]*\]' "$ASPM" | tr -d '[]' || true; }
    cmdline_value() { grep -o "$1=[^ ]*" "$CMDLINE" | tail -n 1 | cut -d= -f2 || true; }
    # Set the ASPM policy with every PCI function awake. A runtime-suspended
    # function puts back the link control it saved before the change when it
    # next resumes, and a multi-function link only enables what all of its
    # functions enable, so changing it under suspended functions left the
    # internal GPU/USB/audio and USB4 links without ASPM. The kernel ignores a
    # write of the current policy, so a reapply goes through another one.
    # Written to work with or without errexit (sleep-safe calls it with ||).
    set_aspm() {
      local c value now
      controls=$(mktemp) || return 1
      for c in "$SYS"/bus/pci/devices/*/power/control; do
        value=$(cat "$c" 2>/dev/null) || continue
        echo "$c $value" >> "$controls"
        echo on > "$c" 2>/dev/null || true
      done
      if [ "$(aspm_now)" = "$1" ]; then
        if [ "$1" = performance ]; then echo default; else echo performance; fi > "$ASPM" || true
      fi
      echo "$1" > "$ASPM" || true
      put_back_controls
      now=$(aspm_now)
      [ "$now" = "$1" ] || { log "aspm reads $now after writing $1" err; return 1; }
    }
    shift || true
    case "$cmd" in
      status)
        v=$(cat /sys/class/power_supply/BAT1/voltage_now); i=$(cat /sys/class/power_supply/BAT1/current_now)
        echo "battery: $(cat /sys/class/power_supply/BAT1/status) $(awk -v v="$v" -v i="$i" 'BEGIN{printf "%.2f W", v*i/1e12}') ($(cat /sys/class/power_supply/BAT1/capacity)%)"
        for h in /sys/class/drm/card*/device/hwmon/hwmon*/power1_average; do echo "soc: $(awk '{printf "%.2f W",$1/1e6}' "$h")"; done
        echo "aspm: $(cat /sys/module/pcie_aspm/parameters/policy)"
        for p in /sys/class/drm/card*-eDP-1/amdgpu/panel_power_savings; do echo "abm: $(cat "$p")"; done
        echo "boost: global=$(cat /sys/devices/system/cpu/cpufreq/boost) cpu0=$(cat /sys/devices/system/cpu/cpu0/cpufreq/boost 2>/dev/null || echo n/a)"
        echo "epp: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference) governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor) platform: $(cat /sys/firmware/acpi/platform_profile)"
        for f in /sys/kernel/debug/dri/*/eDP-1/psr_state /sys/kernel/debug/dri/*/eDP-1/psr_capability; do [ -r "$f" ] && echo "$(basename "$f"): $(tr '\n' ' ' < "$f")"; done
        [ -e "$SLEEPING" ] && echo "sleeping: $(cat "$SLEEPING") (sleep-safe ran, sleep-restore not yet)"
        [ -r /sys/kernel/debug/amd_pmc/s0ix_stats ] && { echo "s0ix_stats:"; sed 's/^/  /' /sys/kernel/debug/amd_pmc/s0ix_stats; }
        [ -r /sys/kernel/debug/dri/1/amdgpu_pm_info ] && grep -E 'GFX Clocks|Clocks|power|Power|GPU Load|MCLK|SCLK' /sys/kernel/debug/dri/1/amdgpu_pm_info | sed 's/^/  pm_info: /' | head -12
        exit 0
        ;;
      powertop)
        secs=''${1:-20}
        [[ "$secs" =~ ^[0-9]{1,4}$ ]] || usage
        out=$(mktemp /tmp/powertop-XXXXXX.csv)
        powertop --csv="$out" --time="$secs" >/dev/null 2>&1 || true
        chmod 644 "$out"; echo "$out"
        ;;
      turbostat)
        secs=''${1:-10}
        [[ "$secs" =~ ^[0-9]{1,4}$ ]] || usage
        turbostat --quiet --Summary --interval "$secs" --num_iterations 1 2>&1
        ;;
      dmesg)
        # Read-only flags only: no --clear/--read-clear, console level or console
        # on/off, no -F/-K (read a file as root); never a pager.
        args=(--nopager)
        while [ $# -gt 0 ]; do
          case "$1" in
            -T|--ctime|-t|--notime|-x|--decode|-k|--kernel|-u|--userspace|-r|--raw|-J|--json|-w|--follow|-W|--follow-new|-e|--reltime|-d|--show-delta|-S|--syslog|-P|--nopager|-L|--color=never|--color=always)
              args+=("$1") ;;
            -l|--level|-f|--facility|--since|--until|--time-format)
              [ $# -ge 2 ] && [[ "$2" =~ ^[A-Za-z0-9:,.@\ +-]+$ ]] || usage
              args+=("$1" "$2"); shift ;;
            --level=*|--facility=*|--since=*|--until=*|--time-format=*)
              [[ "''${1#*=}" =~ ^[A-Za-z0-9:,.@\ +-]+$ ]] || usage
              args+=("$1") ;;
            *) echo "power-lab dmesg: refusing $1 (read-only flags only)" >&2; exit 2 ;;
          esac
          shift
        done
        dmesg "''${args[@]}"
        ;;
      trigger)
        [ $# -eq 1 ] && [ "$1" = power_supply ] || usage
        begin_change
        udevadm trigger --action=change --settle -s power_supply; echo triggered
        ;;
      lspci)
        args=()
        while [ $# -gt 0 ]; do
          case "$1" in
            -v|-vv|-vvv|-n|-nn|-k|-t|-tv|-tvv|-D|-m|-mm|-q|-qq) args+=("$1") ;;
            -s|-d) [ $# -ge 2 ] && [[ "$2" =~ ^[0-9a-fA-F:.*]+$ ]] || usage; args+=("$1" "$2"); shift ;;
            *) echo "power-lab lspci: refusing $1 (read-only flags only)" >&2; exit 2 ;;
          esac
          shift
        done
        lspci "''${args[@]}"
        ;;
      abm)
        [ $# -eq 1 ] && [[ "$1" =~ ^[0-4]$ ]] || usage
        begin_change
        for p in "$SYS"/class/drm/card*-eDP-1/amdgpu/panel_power_savings; do echo "$1" > "$p"; echo "$p=$(cat "$p")"; done
        ;;
      aspm)
        [ $# -eq 1 ] && [[ "$1" =~ ^(default|performance|powersave|powersupersave)$ ]] || usage
        begin_change
        set_aspm "$1"; cat "$ASPM"
        ;;
      boost)
        # Per CPU, with the global switch left on: power-profiles-daemon writes
        # the per-CPU files on every profile change (off for power-saver), and
        # the kernel refuses those writes while boost is off globally.
        [ $# -eq 1 ] && [[ "$1" =~ ^[01]$ ]] || usage
        begin_change
        echo 1 > "$SYS"/devices/system/cpu/cpufreq/boost
        for p in "$SYS"/devices/system/cpu/cpufreq/policy*/boost; do echo "$1" > "$p"; done
        echo "boost=$1 max_mhz=$(( $(cat "$SYS"/devices/system/cpu/cpu0/cpufreq/scaling_max_freq) / 1000 ))"
        ;;
      apst)
        [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]{1,9}$ ]] || usage
        begin_change
        for f in "$SYS"/class/nvme/nvme*/power/pm_qos_latency_tolerance_us; do echo "$1" > "$f"; echo "$f=$(cat "$f")"; done
        ;;
      wifips)
        [ $# -eq 1 ] && [[ "$1" =~ ^(on|off)$ ]] || usage
        begin_change
        for i in "$SYS"/class/net/*; do
          [ -d "$i/wireless" ] || [ -e "$i/phy80211" ] || continue
          iw dev "$(basename "$i")" set power_save "$1"; iw dev "$(basename "$i")" get power_save
        done
        ;;
      gpudpm)
        [ $# -eq 1 ] && [[ "$1" =~ ^(auto|low)$ ]] || usage
        begin_change
        for f in "$SYS"/class/drm/card*/device/power_dpm_force_performance_level; do echo "$1" > "$f"; echo "$f=$(cat "$f")"; done
        ;;
      cpucap)
        # none = the highest the CPU can go (amd_pstate_max_freq, with boost):
        # the cap then follows boost, which sets the CPU's own maximum.
        [ $# -eq 1 ] && [[ "$1" =~ ^([0-9]{3,4}|none)$ ]] || usage
        begin_change
        for p in "$SYS"/devices/system/cpu/cpufreq/policy*; do
          if [ "$1" = none ]; then cat "$p/amd_pstate_max_freq" 2>/dev/null || cat "$p/cpuinfo_max_freq"; else echo "$1"000; fi > "$p/scaling_max_freq"
        done
        echo "max_mhz=$(( $(cat "$SYS"/devices/system/cpu/cpu0/cpufreq/scaling_max_freq) / 1000 ))"
        ;;
      xhci-reset)
        # A dead xHCI controller ("HC died; cleaning up": every USB device on it
        # gone) comes back by rebinding the driver, without a reboot.
        [ $# -le 1 ] || usage
        bdf=''${1:-0000:c1:00.3}
        [[ "$bdf" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]] || usage
        dev=$SYS/bus/pci/devices/$bdf
        driver=$SYS/bus/pci/drivers/xhci_hcd
        [ -e "$dev" ] || { echo "power-lab: no PCI device $bdf" >&2; exit 1; }
        bound=""
        [ -L "$dev/driver" ] && bound=$(basename "$(readlink "$dev/driver")")
        # Bound to xhci_hcd, or unbound (a reset that stopped half-way) and an xHCI controller by class.
        if [ "$bound" != xhci_hcd ] && { [ -n "$bound" ] || [ "$(cat "$dev/class")" != 0x0c0330 ]; }; then
          echo "power-lab: $bdf is not an xHCI controller (driver: ''${bound:-none})" >&2; exit 1
        fi
        begin_change
        log "xhci-reset $bdf (driver: ''${bound:-none})"
        if [ "$bound" = xhci_hcd ]; then echo "$bdf" > "$driver/unbind"; sleep 1; fi
        echo "$bdf" > "$driver/bind"
        bound=""
        [ -L "$dev/driver" ] && bound=$(basename "$(readlink "$dev/driver")")
        echo "$bdf: driver=''${bound:-none}"
        [ "$bound" = xhci_hcd ]
        ;;
      sleep-safe)
        # power-experiment (an experiment block, or an applied combination) may
        # have relaxed the s2idle crash workarounds while awake; put back what
        # the kernel command line asked for (and GPU DPM auto), noting what was
        # there for the log. The crashes happen on the way into s2idle.
        # The flag first: from now on changes refuse, then wait for one under way.
        hooks_only
        mkdir -p "$RUN/power-lab"
        date +%s > "$SLEEPING"
        lock 30 || log "sleep-safe: proceeding without the lock" warning
        saved=$RUN/power-lab/pre-sleep; : > "$saved"
        failed=0
        policy=$(cmdline_value pcie_aspm.policy)
        current=$(aspm_now)
        if [ -n "$policy" ] && [ "$policy" != "$current" ]; then
          echo "aspm $current" >> "$saved"
          set_aspm "$policy" || failed=1
        fi
        latency=$(cmdline_value nvme_core.default_ps_max_latency_us)
        if [ -n "$latency" ]; then
          for f in "$SYS"/class/nvme/nvme*/power/pm_qos_latency_tolerance_us; do
            value=$(cat "$f")
            if [ "$value" != "$latency" ]; then
              echo "apst $f $value" >> "$saved"
              echo "$latency" > "$f" || failed=1
            fi
          done
        fi
        for f in "$SYS"/class/drm/card*/device/power_dpm_force_performance_level; do
          value=$(cat "$f")
          if [ "$value" != auto ]; then
            echo "gpudpm $f $value" >> "$saved"
            echo auto > "$f" || failed=1
          fi
        done
        [ "$failed" -eq 0 ] || { log "sleep-safe: a workaround could not be put back before suspend" err; exit 1; }
        ;;
      sleep-restore)
        # After resume (post-resume hook): end the lock-out. What sleep-safe put
        # back stays: these are the risky settings, and power-experiment sets
        # them again itself, after its post-resume wait and within its hold and
        # daily limit (an experiment block, or the reconciler for applied ones).
        hooks_only
        lock 30 || log "sleep-restore: proceeding without the lock" warning
        saved=$RUN/power-lab/pre-sleep
        if [ -s "$saved" ]; then
          log "after resume, left at the boot-time workarounds: $(tr '\n' ';' < "$saved")"
        fi
        rm -f "$SLEEPING" "$saved"
        echo "resumed: aspm=$(aspm_now)"
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
      *) usage ;;
    esac
  '';
in {
  environment.systemPackages = [powerLab];
  # Run as root around every suspend (sleep-actions.service: powerDownCommands is
  # its start, resumeCommands its stop): the crash workarounds hold on the way
  # into s2idle, and stay after resume until power-experiment sets its settings
  # again. Failures are logged (journal, tag power-lab); `|| true` keeps the
  # rest of the hooks running.
  powerManagement.powerDownCommands = "${powerLab}/bin/power-lab sleep-safe || true";
  powerManagement.resumeCommands = "${powerLab}/bin/power-lab sleep-restore || true";
  # The behaviour tests (fake /sys, /run and /proc/cmdline; stubbed tools) and
  # shellcheck, at build time: a power-lab that fails them fails the rebuild.
  system.checks = [
    (pkgs.runCommand "power-lab-check" {nativeBuildInputs = [pkgs.shellcheck];} ''
      shellcheck --severity=style ${powerLab}/bin/power-lab
      shellcheck --shell=bash ${./power-lab-test.sh}
      ${pkgs.bash}/bin/bash ${./power-lab-test.sh} ${powerLab}/bin/power-lab
      touch $out
    '')
  ];
  security.sudo-rs.extraRules = [
    {
      users = ["zyansheep"];
      runAs = "root";
      commands = [
        {
          command = "/run/current-system/sw/bin/power-lab";
          options = ["NOPASSWD"];
        }
      ];
    }
  ];
}
