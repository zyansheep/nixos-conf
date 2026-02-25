# Core system configuration - applied to all hosts
{inputs, ...}: {
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
      findutils
    ];
  };

  nix = {
    package = pkgs.nixVersions.stable;
    settings = let
      GB = 1024 * 1024 * 1024;
    in {
      # Prevents impurities in builds
      sandbox = lib.mkDefault true;

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

    nixPath = ["nixpkgs=${pkgs.path}" "home-manager=flake:home"];

    # Registry setup - use lib.mkForce on to attribute to override NixOS module defaults
    registry = {
      home.flake = inputs.home;
      nixpkgs.to = lib.mkForce {
        type = "path";
        path = inputs.latest.outPath;
      };
      stable.flake = inputs.stable;
      nixos-hardware.flake = inputs.nixos-hardware;
    };
  };
}
