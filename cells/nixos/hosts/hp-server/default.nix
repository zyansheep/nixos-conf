{ suites, profiles, lib, config, pkgs, ... }:
with lib;
{
  imports =
    with profiles; [
      # Include the results of the hardware scan.
      ./hardware-configuration.nix

      core.minimal
      services.containers
      core.tools
      services.ssh
      development.shell.zsh
    ] ++ suites.base;

  # Bootloader
  boot.loader.grub.enable = true;
  boot.loader.grub.version = 2;
  boot.loader.grub.device = "/dev/disk/by-id/wwn-0x600508b1001c779cf2d23254a9610eb9";
  boot.loader.timeout = 1;

  services.openssh.ports = [ 22022 ];

  # Hostname
  networking.hostName = "hp-server";

  bud.enable = true;
  bud.localFlakeClone = "/root/devos-conf";
}
