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
  # Key-based auth only. With the firewall off across the fleet, passwords
  # would be the only barrier protecting the wheel account from brute
  # force (incl. over IPv6, router permitting), so remove them entirely.
  services.openssh.settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
  };
}
