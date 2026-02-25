_: {
  lib,
  pkgs,
  ...
}: {
  # programs.tmux.shell = lib.mkForce "${pkgs.zsh}/bin/zsh";

  programs.direnv.enableZshIntegration = true;

  # better cd command (use `z`!)
  programs.zoxide.enable = true;

  programs.zsh = {
    enable = true;
    enableCompletion = true;

    # Instant prompt - must be at very top of .zshrc
    initExtraFirst = ''
      # Enable Powerlevel10k instant prompt (shows prompt immediately while rest loads)
      if [[ -r "''${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-''${(%):-%n}.zsh" ]]; then
        source "''${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-''${(%):-%n}.zsh"
      fi
    '';

    initContent = ''
      export PATH="$HOME/.local/bin:$HOME/.npm-packages/bin:$PATH"

      # Source local overrides (no rebuild needed)
      [[ -f ~/.zshrc.local ]] && source ~/.zshrc.local
    '';

    history = {
      extended = true;
      ignoreDups = true;
      ignorePatterns = ["&" "[bf]g" "c" "clear" "history" "exit" "q" "pwd" "* --help"];
      ignoreSpace = true;
      share = false;
    };

    oh-my-zsh = {
      enable = true;

      plugins = [
        "git"
        "tmux"
      ];
    };

    plugins = [
      {
        name = "powerlevel10k";
        src = pkgs.zsh-powerlevel10k;
        file = "share/zsh-powerlevel10k/powerlevel10k.zsh-theme";
      }
      {
        name = "powerlevel10k-config";
        src = lib.cleanSource ./_files/p10k-config;
        file = "p10k.zsh";
      }
    ];

    localVariables = {
      ZSH_DISABLE_COMPFIX = "true";
      COMPLETION_WAITING_DOTS = "true";
      DISABLE_UNTRACKED_FILES_DIRTY = "true";
      HIST_STAMPS = "dd.mm.yyyy";
    };
  };
}
