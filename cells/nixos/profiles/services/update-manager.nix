# update-manager (~/Projects/UpdateManager, the update-manager flake input):
# the Waybar widget for updates across functor and isomorph.
#
# - update-manager-agent (user service, every machine): gathers this machine's
#   system and what nixos-ci built for it, the closure diff and download
#   estimate, profile packages, and (functor) nixos-ci and the upstream checks.
#   It publishes $XDG_RUNTIME_DIR/update-manager/state.json, which the Waybar
#   module and the panel only read. isomorph has no sshd: its agent pushes its
#   state to functor over ssh on every pull, and picks up work queued for it.
# - update-manager-panel (user service, graphical sessions): the resident
#   GTK 4 panel, toggled over D-Bus like drainhound's.
# - update-manager-switch@<system>.service / update-manager-boot@<system>.service
#   (root): fetch a nixos-system-<this host> path (signatures checked) and
#   switch to it, or make it the boot default. zyansheep may start them through
#   polkit, which is what "Switch" in the panel does, locally or remotely.
#   That is root-equivalent, as `sudo rebuild` (core.rebuild) already is.
# - `nrb-request REASON [rebuild args]` (= `update-manager ask-rebuild`): how coding
#   agents ask to switch. A full-screen window shows their reason and what changes
#   (built without root by `rebuild --build-only`); Approve runs `sudo rebuild`.
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: {
  imports = [inputs.update-manager.nixosModules.default];

  environment.systemPackages = [
    (pkgs.writeShellScriptBin "nrb-request" ''
      exec ${config.services.update-manager.package}/bin/update-manager ask-rebuild "$@"
    '')
  ];

  services.update-manager = {
    enable = true;
    users = ["zyansheep"];
    # The switch units fetch CI builds from functor's cache (its key is trusted
    # by services.functor-cache); on functor itself they are already in the store.
    substituters = lib.optional (config.networking.hostName != "functor") "http://100.64.0.4:5000?priority=50";
    settings = {
      ci_host = "functor";
      cache_url = "http://100.64.0.4:5000";
      forks_host = "functor";
      hosts.functor = {
        ssh = "functor";
        pull = true;
        label = "Desktop · CI · cache";
      };
      # No sshd on isomorph (services.ssh is off): it reports in to functor instead.
      hosts.isomorph = {
        ssh = "isomorph";
        pull = false;
        label = "Framework laptop";
      };
    };
  };
}
