# Starfish Shell Prompt Configuration
{
  self,
  lib,
  config,
  pkgs,
  ...
}: let
  inherit (lib) fileContents;
in {
  environment = {
    systemPackages = [pkgs.starship];
    shellInit = ''
      export STARSHIP_CONFIG=${
        pkgs.writeText "starship.toml"
        (fileContents ./starship.toml)
      }
    '';
  };

  programs.bash = {
    promptInit = ''
      eval "$(${pkgs.starship}/bin/starship init bash)"
    '';
  };
  programs.zsh = {
    promptInit = ''
      eval "$(${pkgs.starship}/bin/starship init zsh)"
    '';
  };
}
