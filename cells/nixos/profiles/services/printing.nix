{
  inputs,
  common,
}: { lib, config, pkgs, ... }:
with lib;
{
	services.printing.enable = true;
	services.printing.drivers = [ pkgs.gutenprint ];
}
