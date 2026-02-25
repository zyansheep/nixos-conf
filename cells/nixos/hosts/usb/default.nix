{
  inputs,
  suites,
  profiles,
  lib,
  modulesPath,
  ...
}: {
  imports = with profiles; [
    suites.base

    # Import the installer CD base modules for proper filesystem setup
    "${modulesPath}/installer/cd-dvd/installation-cd-minimal.nix"

    core.minimal
    core.common
    core.installation-cd-base
  ];

  # Override hostname for live system
  networking.hostName = lib.mkForce "live";
  networking.firewall.enable = false;

  security.doas.extraRules = [
    {
      users = ["zyansheep"];
      keepEnv = true;
      persist = true;
    }
  ];

  hardware.enableAllFirmware = true;

  documentation.info.enable = false;

  system.stateVersion = "24.05";
}
