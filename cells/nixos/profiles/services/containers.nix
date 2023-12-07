{
  inputs,
  common,
}: { lib, config, pkgs, ... }:
with lib;
{
  virtualisation.podman = {
    dockerCompat = true;
    enable = true;
  };
  environment.systemPackages = with pkgs; [
    # Easily create Docker containers from Nix files
    arion

    # Install the docker CLI to talk to podman.
    # Not needed when virtualisation.docker.enable = true;
    docker-client

    podman-compose
  ];
}
