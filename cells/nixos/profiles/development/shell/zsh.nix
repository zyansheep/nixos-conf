# My Zsh Configuration
{
  config,
  pkgs,
  ...
}: {
  programs.zsh = {
    enable = true;
    # enableCompletion = true;
    # autosuggestions.enable = true;
    /*
     ohMyZsh = {
     			enable = true;
     			plugins = ["git" "python" "man"];
     			package = pkgs.zsh-powerlevel10k;
     			theme = "powerlevel10k";
    };
    */
  };
  users.defaultUserShell = pkgs.zsh;
}
