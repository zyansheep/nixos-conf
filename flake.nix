{
  description = "The Hive - The secretly open NixOS-Society";

  nixConfig.extra-experimental-features = "nix-command flakes";
  nixConfig.extra-substituters = "https://nrdxp.cachix.org https://nix-community.cachix.org";
  nixConfig.extra-trusted-public-keys = "nrdxp.cachix.org-1:Fc5PSqY2Jm1TrWfm88l6cvGWwz3s93c6IOifQWnhNW4= nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs=";

  # common for deduplication
  inputs = {
    flake-utils = {
      url = "github:numtide/flake-utils";
    };
  };

  # hive
  inputs = {
    devshell = {
      url = "github:numtide/devshell";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    nixago = {
      url = "github:nix-community/nixago";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        flake-utils.follows = "flake-utils";
      };
    };

    std = {
      follows = "hive/std";
    };

    hive = {
      url = "github:divnix/hive";
      inputs = {
        colmena.follows = "colmena";
        nixago.follows = "nixago";
        nixpkgs.follows = "nixpkgs";
      };
    };

    haumea = {
      follows = "hive/std/haumea";
    };
  };

  # tools
  inputs = {
    nix-filter.url = "github:numtide/nix-filter";

    nixos-generators.url = "github:nix-community/nixos-generators";
    nixos-generators.inputs.nixpkgs.follows = "nixpkgs";

    nixos-hardware.url = "github:nixos/nixos-hardware";

    colmena = {
      url = "github:zhaofengli/colmena";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        flake-utils.follows = "flake-utils";
      };
    };

    lanzaboote = {
      # secure boot
      url = "github:nix-community/lanzaboote/v0.3.0";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    nix-rpi-kernel = {
      url = "github:TrueLecter/nix-rpi-kernel";
    };
  };

  # nixpkgs & home-manager
  inputs = {
    latest.url = "github:nixos/nixpkgs/nixos-unstable";
    nixos.url = "github:nixos/nixpkgs/nixos-24.05";
    nixpkgs.follows = "nixos";

    home = {
      url = "github:nix-community/home-manager/release-24.05";
      inputs.nixpkgs.follows = "nixos";
    };

    home-unstable = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "latest";
    };

    rpi-4-kernel.url = "github:nixos/nixpkgs/refs/pull/292880/merge";
  };

  # tools
  inputs = {
    nix-vscode-extensions = {
      url = "github:nix-community/nix-vscode-extensions";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        flake-utils.follows = "flake-utils";
      };
    };
    nvfetcher = {
      url = "github:berberman/nvfetcher";
      inputs = {
        nixpkgs.follows = "nixpkgs";
        flake-utils.follows = "flake-utils";
      };
    };
    # nix-diff = {
    #   url = "github:Gabriella439/nix-diff";
    #   inputs = {
    #     nixpkgs.follows = "nixpkgs";
    #   };
    # };
  };

  outputs = {
    self,
    std,
    nixpkgs,
    hive,
    ...
  } @ inputs: let
    collect-unrenamed = hive.collect // {renamer = _: target: target;};
    collect-renamed = hive.collect;
  in
    hive.growOn {
      inherit inputs;

      nixpkgsConfig = {
        allowUnfree = true;
      };

      systems = [
        "aarch64-linux"
        "x86_64-linux"
      ];

      cellsFrom = ./cells;

      cellBlocks = with std.blockTypes;
      with hive.blockTypes; [
        (nixago "config")

        # Modules
        (functions "nixosModules")
        (functions "darwinModules")
        (functions "homeModules")

        # Profiles
        (functions "commonProfiles")
        (functions "nixosProfiles")
        (functions "darwinProfiles")
        (functions "homeProfiles")
        (functions "userProfiles")
        (functions "users")

        # Suites
        (functions "nixosSuites")
        (functions "darwinSuites")
        (functions "homeSuites")

        (devshells "shells")

        (functions "lib")

        (files "files")
        (installables "packages")
        (installables "firmwares")
        (pkgs "overrides")
        (functions "overlays")

        colmenaConfigurations
        homeConfigurations
        nixosConfigurations
        darwinConfigurations
      ];
    }
    # soil
    {
      devShells = hive.harvest inputs.self ["repo" "shells"];
      packages = hive.harvest inputs.self [
        ["klipper" "packages"]
        ["common" "packages"]
        ["pam-reattach" "packages"]
      ];

      # nixosModules = hive.pick inputs.self [];

      homeModules = hive.pick inputs.self [
        ["home" "homeModules"]
      ];
    }
    {
      colmenaHive = collect-unrenamed self "colmenaConfigurations";
      nixosConfigurations = collect-unrenamed self "nixosConfigurations";
      homeConfigurations = collect-unrenamed self "homeConfigurations";
      darwinConfigurations = collect-unrenamed self "darwinConfigurations";
    }
    {
      debug = hive.harvest inputs.self ["repo" "debug"];
    };
}
