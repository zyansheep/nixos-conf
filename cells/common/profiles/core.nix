{inputs, ...}: {
  self,
  config,
  lib,
  pkgs,
  ...
}: {
  environment = {
    # Selection of sysadmin tools that can come in handy
    systemPackages = with pkgs; [
      coreutils
      curl
      direnv
      delta
      bat
      git
      bottom
      jq
      tmux
      zsh
      vim
      file
      gnused
      ncdu
      nix-tree
    ];

    shellAliases = {
      # quick cd
      ".." = "cd ..";
      "..." = "cd ../..";
      "cd.." = "cd ..";

      # internet ip
      # TODO: explain this hard-coded IP address
      myip = "dig +short myip.opendns.com @208.67.222.222 2>&1";

      mn = ''
        manix "" | grep '^# ' | sed 's/^# \(.*\) (.*/\1/;s/ (.*//;s/^# //' | sk --preview="manix '{}'" | xargs manix
      '';
      top = "btm";

      mkdir = "mkdir -pv";
      cp = "cp -iv";
      mv = "mv -iv";

      ll = "ls -l";
      la = "ls -la";

      path = "printf \\\"%b\\\\n\\\" \\\"\\\${PATH//:/\\\\\\n}\\\"";
      tm = "tmux new-session -A -s main";

      issh = "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null";

      nix-cleanup = "nix-collect-garbage -d && sudo nix-collect-garbage -d";
    };

    pathsToLink = ["/share/zsh"];

    variables = {
      # vim as default editor
      EDITOR = "vim";
      VISUAL = "vim";

      # Use custom `less` colors for `man` pages.
      LESS_TERMCAP_md = "$(tput bold 2> /dev/null; tput setaf 2 2> /dev/null)";
      LESS_TERMCAP_me = "$(tput sgr0 2> /dev/null)";

      # Don't clear the screen after quitting a `man` page.
      MANPAGER = "less -X";
    };
  };

  nix = {
    settings = let
      GB = 1024 * 1024 * 1024;
    in {
      # Prevents impurities in builds
      sandbox = true;

      # Give root user and wheel group special Nix privileges.
      trusted-users = ["root" "@wheel"];

      keep-outputs = lib.mkDefault true;
      keep-derivations = lib.mkDefault true;
      builders-use-substitutes = true;
      experimental-features = ["flakes" "nix-command"];
      fallback = true;
      warn-dirty = false;

      # Some free space
      min-free = lib.mkDefault (5 * GB);
    };

    # Improve nix store disk usage
    gc = {
      automatic = true;
      options = "--delete-older-than 7d";
    };

    nixPath = [
      "nixpkgs=flake:nixos"
      "home-manager=flake:home"
    ];

    registry = let
      inputs' = lib.filterAttrs (n: _: !(builtins.elem n ["cells" "self" "nixpkgs"])) inputs;
    in
      (lib.mapAttrs (_: v: {flake = v;}) inputs')
      // {
        n.flake = inputs.nixos;
        l.flake = inputs.latest;
      };
  };
}
