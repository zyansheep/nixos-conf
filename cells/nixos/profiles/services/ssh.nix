{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  services.sshd.enable = true;
  # services.openssh.settings.permitRootLogin = "yes";
}
