# /media/scratch: zpool/local/scratch, a dataset with no snapshots (sanoid snapshots only zpool/safe), for data that
# churns and can be rebuilt. Under zpool/safe/home, every file that is deleted or rewritten stays in the snapshots
# for 12 days (and in a yearly one for 4 years), so churn there costs space long after the file is gone: on
# 2026-11-25 the home snapshots held 215 GB, half of it copies of a chat database written and deleted every day.
#
# The user's folders here are linked from their usual places in home:
#   ~/.cache                              -> /media/scratch/zyansheep/cache
#   ~/.local/share/dither-chat/backups    -> /media/scratch/zyansheep/dither-chat-backups
# (the live database stays in home and is snapshotted; these are only its extra copies)
#
# zfs-scratch-dataset makes the dataset the first time, before the mount: without it, the mount failed on the switch
# and nixos-deploy rolled the switch back. nofail, so a boot never waits on it.
{config, ...}: {
  fileSystems."/media/scratch" = {
    device = "zpool/local/scratch";
    fsType = "zfs";
    options = ["nofail" "x-systemd.requires=zfs-scratch-dataset.service"];
  };
  systemd.services.zfs-scratch-dataset = {
    description = "Make zpool/local/scratch (no snapshots) if it is missing";
    after = ["zfs-import.target"];
    before = ["media-scratch.mount"];
    unitConfig.DefaultDependencies = false;
    path = [config.boot.zfs.package];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      zfs list -H -o name zpool/local/scratch >/dev/null 2>&1 || zfs create -o mountpoint=legacy zpool/local/scratch
    '';
  };
  # The user's folder, made once the dataset is mounted (a tmpfiles rule could run first and land under the mount).
  systemd.services.scratch-user-dir = {
    description = "Give zyansheep a folder on /media/scratch";
    after = ["media-scratch.mount"];
    requires = ["media-scratch.mount"];
    wantedBy = ["media-scratch.mount"];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "/run/current-system/sw/bin/install -d -m 700 -o zyansheep -g users /media/scratch/zyansheep";
    };
  };
}
