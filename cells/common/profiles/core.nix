{ inputs, cell, ... }:
{ self, config, lib, pkgs, ... }: {
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
      findutils
    ];
  };

  nix = {
    package = pkgs.nixVersions.stable;
    # extraOptions = ''
    #   experimental-features = nix-command flakes
    # '';
    settings = let GB = 1024 * 1024 * 1024;
    in {
      # Prevents impurities in builds
      sandbox = lib.mkDefault true;

      # Give root user and wheel group special Nix privileges.
      trusted-users = [ "root" "@wheel" ];

      keep-outputs = lib.mkDefault true;
      keep-derivations = lib.mkDefault true;
      builders-use-substitutes = true;
      experimental-features = [ "flakes" "nix-command" ];
      fallback = true;
      warn-dirty = false;

      # Some free space
      min-free = lib.mkDefault (5 * GB);
    };

    # Improve nix store disk usage
    /* gc = {
         automatic = true;
         options = "--delete-older-than 7d";
       };
    */

    nixPath = [ "nixpkgs=${pkgs.path}" "home-manager=flake:home" ];

    registry = {
      home.flake = inputs.home;
      l.flake = inputs.latest;
      u.flake = inputs.nixpkgs-unfree;
      firefox-nightly.flake = inputs.firefox;
      nixpkgs.flake = inputs.nixos; # stable
      nixos-hardware.flake = inputs.nixos-hardware;
    };
  };
}
