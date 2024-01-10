{
  config,
  pkgs,
  ...
}: let
	framework-laptop-kmod = pkgs.linuxPackages_latest.callPackage ./framework-laptop-kmod.nix {};
in {
  boot.kernelPackages = pkgs.linuxPackages_latest; # Use latest kernel for latest improvements
  
  boot.extraModulePackages = [ framework-laptop-kmod ]; # Expose battery charge limits to userspace (and other stuff)
  boot.kernelModules = [ "framework-laptop-kmod" ];
  # boot.initrd.availableKernelModules = [ "framework-laptop-kmod" ];
  # boot.initrd.kernelModules = [ "framework-laptop-kmod" ];
}
