{
  inputs,
  cell,
}: let
  inherit (inputs) nixpkgs std;
  l = nixpkgs.lib // builtins;

  inherit
    (inputs.cells.common.overrides)
    nix
    ;

  inherit
    (nixpkgs)
    alejandra
    editorconfig-checker
    ;

  pkgWithCategory = category: package: {inherit package category;};
  nixC = pkgWithCategory "nix";
  linter = pkgWithCategory "linter";
  infra = pkgWithCategory "infra";

  inherit (cell) config;
in
  l.mapAttrs (_: std.lib.dev.mkShell) {
    default = {
      name = "infra";

      imports = [
        ./_sops.nix
        std.std.devshellProfiles.default
      ];

      # Import nixago from config
      nixago = [
        config.treefmt
        config.editorconfig
        # config.mdbook
      ];

      commands = [
        (nixC nix)

        (infra inputs.home.packages.home-manager)
        (infra inputs.nixos-generators.packages.nixos-generate)

        (linter editorconfig-checker)
        (linter alejandra)
      ];
    };
  }
