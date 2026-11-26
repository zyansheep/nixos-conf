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
# The dataset is made once by hand: `zfs create -o mountpoint=legacy zpool/local/scratch`. nofail, so a host
# without it still boots.
{...}: {
  fileSystems."/media/scratch" = {
    device = "zpool/local/scratch";
    fsType = "zfs";
    options = ["nofail"];
  };
  systemd.tmpfiles.rules = ["d /media/scratch/zyansheep 0700 zyansheep users -"];
}
