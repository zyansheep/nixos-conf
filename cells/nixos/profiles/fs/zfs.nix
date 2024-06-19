{
  inputs,
  common,
}: { lib, pkgs, config, ... }:

let
  cfg = config.zfsConfig;
in
{
  options.zfsConfig = {
    enableSystemdRollback = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable rollback of ZFS datasets to a pristine state";
    };
    poolName = lib.mkOption {
      type = lib.types.string;
      default = "zpool";
      description = "Default zfs drive name";
    };
  };

  config = lib.mkMerge [
    {
      boot = {
        kernelParams = ["nohibernate"];
        zfs = {
          forceImportRoot = false;
          package = pkgs.zfs_unstable;
        };
        supportedFilesystems = ["zfs"];
      };

      services.zfs = {
        # autoSnapshot.enable = true;
        # autoSnapshot.monthly = 1;
        autoScrub.enable = true;
        trim.enable = true;
      };

      networking.hostId = lib.mkDefault (abort "ZFS requires networking.hostId to be set");

      # Set schedulers for ZFS drives
      services.udev.extraRules = ''
        ACTION=="add|change", KERNEL=="sd[a-z]*[0-9]*|mmcblk[0-9]*p[0-9]*|nvme[0-9]*n[0-9]*", ENV{ID_FS_TYPE}=="zfs_member", ATTR{../queue/scheduler}="none"
      '';
    }

    (lib.mkIf cfg.enableSystemdRollback {
      boot.initrd.systemd.enable = lib.mkDefault true;
      boot.initrd.systemd.services.rollback = {
        description = "Rollback ZFS datasets to a pristine state";
        wantedBy = [
          "initrd.target"
        ];
        after = [
          "zfs-mount-zpool.service"
        ];
        before = [ 
          "sysroot.mount"
        ];
        path = with pkgs; [
          zfs_unstable
        ];
        unitConfig.DefaultDependencies = "no";
        serviceConfig.Type = "oneshot";
        script = ''
          zfs rollback -r zpool/local/root@blank && echo "rollback complete"
        '';
      };
    })
  ];
}
