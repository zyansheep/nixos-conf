{
  # I believe this sets DIRENV_WARN_TIMEOUT to warn user when direnv activation is taking too long
  xdg.configFile."direnv/direnv.toml".text = ''
    [global]
    warn_timeout = "2m"
  '';

  programs.direnv = {
    enable = true;
    nix-direnv = {
      enable = true;
    };
  };
}
