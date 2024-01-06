{
  config,
  pkgs,
  ...
}: let
	framework-laptop-kmod = config.boot.kernelPackages.callPackage ./framework-laptop-kmod.nix {};
in {
  boot.extraModulePackages = [ framework-laptop-kmod ]; # Expose battery charge limits to userspace (and other stuff)
  boot.initrd.availableKernelModules = [ "framework-laptop-kmod" ];
}
