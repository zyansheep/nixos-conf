_:
{ config, pkgs, ... }:
{
  # Let the official GUI own login, server selection, reconnection and the
  # kill switch. Its NetworkManager profiles are implementation details;
  # do not activate them separately or run a competing CLI controller.
  environment.systemPackages = [ pkgs.proton-vpn ];

  assertions = [
    {
      assertion = config.networking.networkmanager.enable;
      message = "The Proton VPN app requires NetworkManager.";
    }
  ];
}
