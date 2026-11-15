{
  programs.delta = {
    enable = true;
    enableGitIntegration = true;
  };

  programs.git = {
    enable = true;

    lfs.enable = true;

    # Every commit is signed with the host's SSH key (git's ssh format, no gpg). nixos-deploy refuses to switch to
    # commits on main that aren't signed by a key in _files/git-allowed-signers, and GitHub requires signed commits
    # on nixos-conf's main, so a stolen GitHub token can't push code that runs as root on these hosts.
    signing = {
      format = "ssh";
      key = "~/.ssh/id_ed25519.pub";
      signByDefault = true;
    };

    ignores = [
      # Linux
      "*~"

      # MacOS
      "._*"
      ".AppleDouble"
      ".DS_Store"
      ".localized"
      ".LSOverride"
      ".Spotlight-V100"
      ".Trashes"

      # Windows
      "Desktop.ini"
      "ehthumbs.db"
      "Thumbs.db"

      # NodeJS
      "node_modules"
      "npm-debug.log*"
      "yarn-debug.log*"
      "yarn-error.log*"

      # vim
      "%*"
      "*.sw[a-z]"
      "*.un~"
      ".netrwhist"
      "Session.vim"
    ];

    settings = {
      init.defaultBranch = "master";
      pretty.custom = "%C(magenta)%h%C(red)%d %C(yellow)%ar %C(green)%s %C(yellow)(%an)";

      pull.rebase = false;
      push.followTags = true;

      user.useConfigOnly = true;

      # `git log --show-signature` / `git verify-commit` know which keys are ours.
      gpg.ssh.allowedSignersFile = "${./_files/git-allowed-signers}";

      core = {
        abbrev = 12;
        autocrlf = "input";
        editor = "vim";
      };

      "diff \"bin\"".textconv = "hexdump --canonical --no-squeezing";

      # Autocorrent in 2 seconds
      help.autocorrect = 20;

      alias = {
        git = "!exec git";

        # reset
        soft = "reset --soft";
        hard = "reset --hard";
        s1ft = "soft HEAD~1";
        h1rd = "hard HEAD~1";

        # logging
        lg = "log --color --graph --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr) %C(bold blue)<%an>%Creset' --abbrev-commit";
        plog = "log --graph --pretty='format:%C(red)%d%C(reset) %C(yellow)%h%C(reset) %ar %C(green)%aN%C(reset) %s'";
        tlog = "log --stat --since='1 Day Ago' --graph --pretty=oneline --abbrev-commit --date=relative";
        rank = "shortlog -sn --no-merges";

        # delete merged branches
        bdm = "!git branch --merged | grep -v '*' | xargs -n 1 git branch -d";
      };
    };

    includes = [
      {path = "~/.gitconfig.local";}
    ];
  };
}
