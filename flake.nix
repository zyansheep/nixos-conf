{
  description = "Zyan's Nix Config";

  nixConfig.extra-experimental-features = "nix-command flakes";
  nixConfig.extra-substituters =
    "https://nrdxp.cachix.org https://nix-community.cachix.org";
  nixConfig.extra-trusted-public-keys =
    "nrdxp.cachix.org-1:Fc5PSqY2Jm1TrWfm88l6cvGWwz3s93c6IOifQWnhNW4= nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs=";

  inputs = {
    # Flake framework
    flake-parts.url = "github:hercules-ci/flake-parts";
    haumea.url = "github:nix-community/haumea";

    # Nixpkgs
    nixpkgs.url = "github:nixos/nixpkgs/nixos-25.11"; # Required by flake-parts
    latest.url = "github:numtide/nixpkgs-unfree/nixos-unstable";
    stable.follows = "nixpkgs";

    # Home Manager
    home.url = "github:nix-community/home-manager/release-25.11";
    home.inputs.nixpkgs.follows = "stable";

    home-unstable.url = "github:nix-community/home-manager";
    home-unstable.inputs.nixpkgs.follows = "latest";

    # Hardware
    nixos-hardware.url = "github:nixos/nixos-hardware";

    # Tools
    lanzaboote.url = "github:nix-community/lanzaboote/v0.4.3";
    lanzaboote.inputs.nixpkgs.follows = "stable";

    impermanence.url = "github:nix-community/impermanence";

    nixos-generators.url = "github:nix-community/nixos-generators";
    nixos-generators.inputs.nixpkgs.follows = "stable";

    nvfetcher.url = "github:berberman/nvfetcher";
    nvfetcher.inputs.nixpkgs.follows = "stable";

    firefox.url = "github:nix-community/flake-firefox-nightly";
    firefox.inputs.nixpkgs.follows = "latest";

    flake-programs-sqlite.url = "github:wamserma/flake-programs-sqlite";
    flake-programs-sqlite.inputs.nixpkgs.follows = "latest";
  };

  outputs = inputs @ {
    self,
    flake-parts,
    haumea,
    nixpkgs,
    stable,
    latest,
    home,
    home-unstable,
    ...
  }:
    flake-parts.lib.mkFlake {inherit inputs;} ({...}: let
      # ============================================================
      # Library functions for loading profiles
      # ============================================================
      lib = import ./lib.nix {inherit inputs haumea;};

      # ============================================================
      # Overlays
      # ============================================================
      mkOverlays = system: let
        latestPkgs = import latest {
          inherit system;
          config.allowUnfree = true;
        };
        sources = (import stable {inherit system;}).callPackage ./cells/common/sources/generated.nix {};
      in {
        common-packages = final: prev:
          lib.importPackages {
            nixpkgs = latestPkgs;
            sources = sources;
            packages = ./cells/common/packages;
          };
        latest-overrides = final: prev: {
          inherit
            (latestPkgs)
            firefox
            vscodium
            alejandra
            nil
            nixpkgs-fmt
            statix
            nix
            cachix
            nix-index
            ffmpeg_5-full
            ;
          nvfetcher = inputs.nvfetcher.packages.${system}.default;
        };
      };

      # ============================================================
      # Load profiles using haumea
      # ============================================================
      loadProfiles = {
        src,
        extraInputs ? {},
      }:
        haumea.lib.load {
          inherit src;
          inputs = {inherit inputs;} // extraInputs;
          transformer = haumea.lib.transformers.liftDefault;
        };

      # Common profiles (shared across NixOS and potentially Darwin)
      commonProfiles = loadProfiles {
        src = ./cells/common/profiles;
      };

      # NixOS profiles
      nixosProfiles = loadProfiles {
        src = ./cells/nixos/profiles;
        extraInputs = {common = commonProfiles;};
      };

      # Home Manager profiles
      homeProfiles = loadProfiles {
        src = ./cells/home/profiles;
        extraInputs = {common = commonProfiles;};
      };

      # ============================================================
      # User profiles (Home Manager suites)
      # ============================================================
      userProfiles = let
        l = stable.lib // builtins;
        suites = with homeProfiles; {
          base = [
            shell.direnv
            git
            dev.codium
            shell.zsh
            shell.nvim
            home-manager-base
            ssh
          ];
          develop = [dev.nix];
        };
      in {
        workstation = {...}: {
          imports = with suites; l.flatten [base develop];
        };
        minimal = {...}: {imports = suites.base;};
        server-dev = {...}: {
          imports = with suites; l.flatten [develop];
        };
      };

      # ============================================================
      # User definitions (NixOS modules that set up users + home-manager)
      # ============================================================
      users = {
        zyansheep = {pkgs, ...}: {
          home-manager.users.zyansheep = {
            imports = [userProfiles.minimal];
            home.stateVersion = "22.11";
          };
          programs.zsh.enable = true;
          users.users.zyansheep = {
            isNormalUser = true;
            shell = pkgs.zsh;
            extraGroups = ["wheel"];
          };
        };
        root = {
          config,
          lib,
          ...
        }: {
          users.users.root = {
            uid = 0;
            # Use mkDefault so installation CD profiles can override with empty password
            initialHashedPassword = lib.mkDefault "\$6\$dnWr7aCjnJFOrj1f\$2Hb5yZCiDTvwgh.qPXSofOH/z30EHO98uwUWxBtkbrbhyXmemsl804l3LC9NX.25aaX/hl0aAIB2hcma822SX/";
          };
        };
      };

      # ============================================================
      # Suites (bundles of profiles)
      # ============================================================
      suites = {
        base = {...}: {
          imports = [
            commonProfiles.core
            users.zyansheep
            users.root
          ];
        };
      };

      # ============================================================
      # All profiles available to hosts
      # ============================================================
      allProfiles =
        nixosProfiles
        // {
          common = commonProfiles;
          users = users;
        };

      # ============================================================
      # Host builder function
      # ============================================================
      mkHost = {
        name,
        system,
        nixpkgs ? latest,
        homeManager ? home-unstable,
        extraModules ? [],
      }: let
        overlays = mkOverlays system;
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
          overlays = [overlays.common-packages overlays.latest-overrides];
        };
      in
        nixpkgs.lib.nixosSystem {
          inherit system;
          specialArgs = {
            inherit inputs;
            profiles = allProfiles;
            inherit suites;
          };
          modules =
            [
              homeManager.nixosModules.home-manager
              {
                nixpkgs.pkgs = pkgs;
                networking.hostName = name;
                home-manager.useGlobalPkgs = true;
                home-manager.useUserPackages = true;
              }
              ./cells/nixos/hosts/${name}
            ]
            ++ extraModules;
        };
    in {
      systems = ["x86_64-linux" "aarch64-linux"];

      flake = {
        # ============================================================
        # Standalone Home Manager (rebuild with: home-manager switch --flake ~/nixos-conf)
        # ============================================================
        homeConfigurations = let
          system = "x86_64-linux";
          overlays = mkOverlays system;
          pkgs = import latest {
            inherit system;
            config.allowUnfree = true;
            overlays = [overlays.common-packages overlays.latest-overrides];
          };
        in {
          zyansheep = home-unstable.lib.homeManagerConfiguration {
            inherit pkgs;
            modules = [
              userProfiles.minimal
              homeProfiles.packages
              {
                home = {
                  username = "zyansheep";
                  homeDirectory = "/home/zyansheep";
                  stateVersion = "22.11";
                };
              }
            ];
          };
        };

        # ============================================================
        # NixOS Configurations
        # ============================================================
        nixosConfigurations = {
          isomorph = mkHost {
            name = "isomorph";
            system = "x86_64-linux";
            nixpkgs = latest;
            homeManager = home-unstable;
          };

          functor = mkHost {
            name = "functor";
            system = "x86_64-linux";
            nixpkgs = latest;
            homeManager = home-unstable;
          };

          hpserver = mkHost {
            name = "hpserver";
            system = "x86_64-linux";
            nixpkgs = latest;
            homeManager = home-unstable;
          };

          dev-admin = mkHost {
            name = "dev-admin";
            system = "x86_64-linux";
            nixpkgs = stable;
            homeManager = home;
          };

          usb = mkHost {
            name = "usb";
            system = "x86_64-linux";
            nixpkgs = stable;
            homeManager = home;
          };
        };
      };

      perSystem = {
        pkgs,
        system,
        ...
      }: let
        overlays = mkOverlays system;
        latestPkgs = import latest {
          inherit system;
          config.allowUnfree = true;
        };
      in {
        # Development shell
        devShells.default = pkgs.mkShell {
          name = "nixos-config";
          packages = with pkgs; [
            latestPkgs.nix
            alejandra
            nil
            home.packages.${system}.home-manager
            inputs.nixos-generators.packages.${system}.nixos-generate
          ];
        };

        # Expose custom packages
        packages = lib.importPackages {
          nixpkgs = latestPkgs;
          sources = pkgs.callPackage ./cells/common/sources/generated.nix {};
          packages = ./cells/common/packages;
        };
      };
    });
}
