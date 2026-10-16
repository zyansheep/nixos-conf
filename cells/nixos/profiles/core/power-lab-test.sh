#!/usr/bin/env bash
# Behaviour tests for power-lab (power-lab.nix) against a fake /sys, /run and
# /proc/cmdline, with logger, udevadm, dmesg, lspci, iw and systemctl stubbed.
# Run at build time by power-lab.nix's system.checks; by hand:
#   bash power-lab-test.sh "$(nix build --no-link --print-out-paths <the power-lab package>)/bin/power-lab"
# Never touches the real /sys or /run: every root the script uses is rewritten.
# check's conditions are strings it evals, so their $… expand there:
# shellcheck disable=SC2016,SC2034
set -u
script=${1:?usage: power-lab-test.sh /nix/store/…-power-lab/bin/power-lab}
unset SUDO_UID SUDO_USER
# The script's own tools (coreutils, util-linux's flock, sed, grep …) for the harness too.
tools=$(sed -n 's/^PATH=\(.*\):\$PATH$/\1/p' "$script")
[ -n "$tools" ] || { echo "no PATH line in $script"; exit 1; }
PATH=$tools:$PATH
bash -n "$script" || exit 1
pass=0; fail=0
check() { # description, condition (eval'd)
  if eval "$2"; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1"; fi
}
setup() {
  ROOT=$(mktemp -d); export ROOT
  mkdir -p "$ROOT"/{sys,run,stubs}
  S=$ROOT/sys
  mkdir -p "$S"/module/pcie_aspm/parameters "$S"/class/nvme/nvme0/power \
    "$S"/class/drm/card1/device "$S"/class/drm/card1-eDP-1/amdgpu "$S"/bus/pci/drivers/xhci_hcd \
    "$S"/bus/pci/devices/0000:c1:00.3/power "$S"/bus/pci/devices/0000:c1:00.0/power "$S"/bus/pci/devices/0000:c1:00.1 \
    "$S"/devices/system/cpu/cpufreq/policy0 "$S"/devices/system/cpu/cpu0
  echo 'default [performance] powersave powersupersave' > "$S"/module/pcie_aspm/parameters/policy
  echo 0 > "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us
  echo auto > "$S"/class/drm/card1/device/power_dpm_force_performance_level
  echo 3 > "$S"/class/drm/card1-eDP-1/amdgpu/panel_power_savings
  echo auto > "$S"/bus/pci/devices/0000:c1:00.3/power/control
  echo auto > "$S"/bus/pci/devices/0000:c1:00.0/power/control
  echo 0x0c0330 > "$S"/bus/pci/devices/0000:c1:00.3/class
  echo 0x030000 > "$S"/bus/pci/devices/0000:c1:00.1/class
  echo 4972000 > "$S"/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq
  echo 4972000 > "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq
  ln -s "$S"/devices/system/cpu/cpufreq/policy0 "$S"/devices/system/cpu/cpu0/cpufreq
  ln -s ../../../bus/pci/drivers/xhci_hcd "$S"/bus/pci/devices/0000:c1:00.3/driver
  : > "$S"/bus/pci/drivers/xhci_hcd/unbind; : > "$S"/bus/pci/drivers/xhci_hcd/bind
  echo 'init=/x pcie_aspm.policy=performance nvme_core.default_ps_max_latency_us=0' > "$ROOT/cmdline"
  echo inactive > "$ROOT/hooks-state"
  for tool in logger udevadm dmesg lspci iw; do
    printf '#!/bin/sh\necho "%s $*" >> "$ROOT/calls"\n' "$tool" > "$ROOT/stubs/$tool"; chmod +x "$ROOT/stubs/$tool"
  done
  # systemctl show -P ActiveState sleep-actions.service: whatever hooks-state says.
  printf '#!/bin/sh\necho "systemctl $*" >> "$ROOT/calls"\ncat "$ROOT/hooks-state"\n' > "$ROOT/stubs/systemctl"
  chmod +x "$ROOT/stubs/systemctl"
  sed -e "s|^SYS=/sys$|SYS=$ROOT/sys|; s|^RUN=/run$|RUN=$ROOT/run|; s|^CMDLINE=/proc/cmdline$|CMDLINE=$ROOT/cmdline|" \
      -e "s|^PATH=|PATH=$ROOT/stubs:|; s|^CHANGE_WAIT=10 |CHANGE_WAIT=1 |" "$script" > "$ROOT/power-lab"
  chmod +x "$ROOT/power-lab"
  for root in "SYS=$ROOT/sys" "RUN=$ROOT/run" "CMDLINE=$ROOT/cmdline" "PATH=$ROOT/stubs:" "CHANGE_WAIT=1 "; do
    grep -q "^$root" "$ROOT/power-lab" || { echo "not replaced in the copy: $root"; exit 1; }
  done
}
lab() { "$ROOT/power-lab" "$@"; }

setup
# Usage and the removed generic writer.
lab sysfs "$S"/power/state mem 2>/dev/null; check "sysfs is gone (usage)" '[ $? -eq 2 ]'
lab trigger pci 2>/dev/null; check "trigger only power_supply" '[ $? -eq 2 ]'
lab trigger power_supply >/dev/null; check "trigger runs udevadm --settle" 'grep -q "udevadm trigger --action=change --settle -s power_supply" "$ROOT/calls"'
check "the lock is root's alone (0600)" '[ "$(stat -c %a "$ROOT/run/power-lab.lock")" = 600 ]'
# dmesg: read-only flags only, never a pager.
for flag in -C -c --clear --read-clear -n 1 -D -E -F /etc/shadow -K -H --console-level=1; do
  lab dmesg "$flag" 2>/dev/null; check "dmesg refuses $flag" '[ $? -eq 2 ]'
done
lab dmesg -T -l err,warn --since '-1h' >/dev/null; check "dmesg passes read-only flags" 'grep -q -- "dmesg --nopager -T -l err,warn --since -1h" "$ROOT/calls"'
lab lspci -xxx 2>/dev/null; check "lspci refuses -xxx" '[ $? -eq 2 ]'
lab lspci -F /etc/shadow 2>/dev/null; check "lspci refuses -F" '[ $? -eq 2 ]'
lab lspci -vv -s c1:00.3 >/dev/null; check "lspci passes -vv -s" 'grep -q "lspci -vv -s c1:00.3" "$ROOT/calls"'
lab powertop 1h 2>/dev/null; check "powertop takes seconds only" '[ $? -eq 2 ]'
lab cpucap 2000 >/dev/null; check "cpucap writes scaling_max_freq" '[ "$(cat "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq)" = 2000000 ]'
lab cpucap none >/dev/null; check "cpucap none: the CPU maximum" '[ "$(cat "$S"/devices/system/cpu/cpufreq/policy0/scaling_max_freq)" = 4972000 ]'
lab cpucap '2000;x' 2>/dev/null; check "cpucap refuses anything else" '[ $? -eq 2 ]'

# set_aspm fails half-way (the fake policy file never shows brackets): power/control is put back and it's logged.
echo 'default performance powersave [powersupersave]' > "$S"/module/pcie_aspm/parameters/policy
lab aspm performance >/dev/null 2>&1; rc=$?
check "a failed aspm change exits non-zero" '[ $rc -ne 0 ] && [ $rc -ne 75 ]'
check "power/control put back after a failure" '[ "$(cat "$S"/bus/pci/devices/0000:c1:00.3/power/control)" = auto ] && [ "$(cat "$S"/bus/pci/devices/0000:c1:00.0/power/control)" = auto ]'
check "the failure is logged" 'grep -q "logger -t power-lab -p user.err -- aspm reads" "$ROOT/calls" && grep -q "aspm failed (exit 1)" "$ROOT/calls"'

# The suspend hooks are not sudo's.
SUDO_UID=1000 lab sleep-safe 2>/dev/null; check "sleep-safe refuses under sudo" '[ $? -eq 2 ] && [ ! -e "$ROOT/run/power-lab/sleeping" ]'
SUDO_UID=1000 lab sleep-restore 2>/dev/null; check "sleep-restore refuses under sudo" '[ $? -eq 2 ]'

# sleep-safe: GPU DPM back to auto, APST to the cmdline value (both noted); the flag makes changes refuse.
echo 'default [performance] powersave powersupersave' > "$S"/module/pcie_aspm/parameters/policy
echo low > "$S"/class/drm/card1/device/power_dpm_force_performance_level
echo 100000 > "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us
echo activating > "$ROOT/hooks-state"
lab sleep-safe; check "sleep-safe succeeds" '[ $? -eq 0 ]'
echo active > "$ROOT/hooks-state"
check "sleep-safe sets gpudpm auto" '[ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = auto ]'
check "sleep-safe sets apst to the cmdline" '[ "$(cat "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us)" = 0 ]'
check "sleep-safe notes gpudpm and apst" 'grep -q "^gpudpm $S/class/drm/card1/device/power_dpm_force_performance_level low$" "$ROOT"/run/power-lab/pre-sleep && grep -q "^apst .* 100000$" "$ROOT"/run/power-lab/pre-sleep'
check "the sleeping flag is set" '[ -e "$ROOT"/run/power-lab/sleeping ]'
check "/run/power-lab is root's alone (0700)" '[ "$(stat -c %a "$ROOT/run/power-lab")" = 700 ]'
lab gpudpm low 2>/dev/null; check "changes refuse while sleeping (75)" '[ $? -eq 75 ]'
lab abm 0 2>/dev/null; check "abm refuses while sleeping" '[ $? -eq 75 ] && [ "$(cat "$S"/class/drm/card1-eDP-1/amdgpu/panel_power_savings)" = 3 ]'
lab xhci-reset 2>/dev/null; check "xhci-reset refuses while sleeping" '[ $? -eq 75 ]'
echo "0 $(( $(date +%s) - 7200 ))" > "$ROOT"/run/power-lab/sleeping
lab gpudpm low 2>/dev/null; check "an old flag still holds while the hooks' unit is up (a long sleep)" '[ $? -eq 75 ]'
echo deactivating > "$ROOT/hooks-state"
lab gpudpm low 2>/dev/null; check "and right after resume, before sleep-restore ran" '[ $? -eq 75 ]'
lab sleep-restore >/dev/null 2>&1; check "sleep-restore succeeds" '[ $? -eq 0 ]'
echo inactive > "$ROOT/hooks-state"
check "after resume gpudpm stays auto" '[ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = auto ]'
check "after resume apst stays at the cmdline value" '[ "$(cat "$S"/class/nvme/nvme0/power/pm_qos_latency_tolerance_us)" = 0 ]'
check "sleep-restore logs what stayed" 'grep -q "after resume, left at the boot-time workarounds: .*gpudpm .*low" "$ROOT/calls"'
check "sleep-restore clears the flag and the note" '[ ! -e "$ROOT"/run/power-lab/sleeping ] && [ ! -e "$ROOT"/run/power-lab/pre-sleep ]'
lab gpudpm low >/dev/null; check "changes work after resume" '[ $? -eq 0 ] && [ "$(cat "$S"/class/drm/card1/device/power_dpm_force_performance_level)" = low ]'

# A stale flag (the hooks' unit is down, sleep-restore never ran) is cleared and logged.
echo activating > "$ROOT/hooks-state"
lab sleep-safe
echo failed > "$ROOT/hooks-state"
lab gpudpm low >/dev/null 2>&1; check "a stale flag is cleared" '[ $? -eq 0 ] && [ ! -e "$ROOT"/run/power-lab/sleeping ]'
check "the stale flag is logged" 'grep -q "clearing a stale sleeping flag" "$ROOT/calls"'
echo inactive > "$ROOT/hooks-state"

# The lock: a change waits CHANGE_WAIT for another, then gives up busy (75).
( flock 8; sleep 3 ) 8< "$ROOT"/run/power-lab.lock &
sleep 0.3
lab cpucap 2000 2>/dev/null; check "a held lock makes changes busy (75)" '[ $? -eq 75 ]'
wait

# xhci-reset: validated BDF, an xHCI controller; unbind, bind.
lab xhci-reset '../../x' 2>/dev/null; check "xhci-reset refuses a bad BDF" '[ $? -eq 2 ]'
lab xhci-reset 0000:c1:00.1 2>/dev/null; check "xhci-reset refuses a non-xHCI device" '[ $? -eq 1 ]'
lab xhci-reset 0000:c1:09.0 2>/dev/null; check "xhci-reset refuses a missing device" '[ $? -eq 1 ]'
lab xhci-reset > "$ROOT/out" 2>/dev/null; check "xhci-reset defaults to c1:00.3" '[ $? -eq 0 ] && grep -q "0000:c1:00.3: driver=xhci_hcd" "$ROOT/out"'
check "xhci-reset unbinds and binds" '[ "$(cat "$S"/bus/pci/drivers/xhci_hcd/unbind)" = 0000:c1:00.3 ] && [ "$(cat "$S"/bus/pci/drivers/xhci_hcd/bind)" = 0000:c1:00.3 ]'
rm "$S"/bus/pci/devices/0000:c1:00.3/driver; : > "$S"/bus/pci/drivers/xhci_hcd/unbind
lab xhci-reset 0000:c1:00.3 >/dev/null 2>&1; check "an unbound xHCI controller is bound (no unbind)" '[ $? -eq 1 ] && [ ! -s "$S"/bus/pci/drivers/xhci_hcd/unbind ]'

rm -rf "$ROOT"
echo "power-lab tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
