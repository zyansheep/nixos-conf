{
  inputs,
  common,
}: { lib, config, pkgs, ... }:
with lib;
{
	programs.steam = {
		enable = true;
		remotePlay.openFirewall = true; # Open ports in the firewall for Steam Remote Play
		dedicatedServer.openFirewall = true; # Open ports in the firewall for Source Dedicated Server
	};
}
