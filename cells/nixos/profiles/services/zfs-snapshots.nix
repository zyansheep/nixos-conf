{
  inputs,
  common,
}: {pkgs, ...}: {
  # zpool/safe and everything under it (home, persist): 12 daily, 1 weekly, 4 yearly; no hourly or monthly (2026-11-25:
  # daily snapshots of /home hold ~5G each, and 130G of snapshots had filled the pool). `weekly` isn't one of the NixOS
  # module's options but its settings are free-form, and sanoid itself knows it.
  services.sanoid = {
    enable = true;
    interval = "hourly";

    datasets = {
      "zpool/safe" = {
        hourly = 0;
        daily = 12;
        weekly = 1;
        monthly = 0;
        yearly = 4;
        autoprune = true;
        autosnap = true;
        recursive = true;
      };
    };
  };

  # Twice a year, Jan 1 and Jul 1, a snapshot kept for good: zpool/safe@biyearly_<date>. Sanoid prunes only its own
  # autosnap_* snapshots, so it never touches these; delete one by hand with `zfs destroy -r zpool/safe@biyearly_<date>`.
  # Persistent: a machine that was off on the day takes it at the next boot (named for that day).
  systemd.services.zfs-biyearly-snapshot = {
    description = "Take the twice-yearly snapshot of zpool/safe (kept for good)";
    after = ["zfs.target"];
    path = [pkgs.coreutils];
    serviceConfig.Type = "oneshot";
    script = ''
      name="zpool/safe@biyearly_$(date +%F)"
      if /run/booted-system/sw/bin/zfs list -H -t snapshot -o name "$name" >/dev/null 2>&1; then
        echo "$name already exists"
      else
        /run/booted-system/sw/bin/zfs snapshot -r "$name" && echo "took $name"
      fi
    '';
  };
  systemd.timers.zfs-biyearly-snapshot = {
    wantedBy = ["timers.target"];
    timerConfig = {
      OnCalendar = "*-01,07-01 00:30:00";
      Persistent = true;
    };
  };
}
