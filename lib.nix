# Simplified library functions for the NixOS configuration
# Replaces cells/common/lib.nix with a standalone version
{
  inputs,
  haumea,
}: let
  inherit (haumea) lib;
in {
  # Import packages from a directory, with optional nvfetcher sources
  importPackages = {
    nixpkgs,
    sources ? null,
    packages,
  }: let
    packagePaths = haumea.lib.load {
      src = packages;
      loader = haumea.lib.loaders.path;
    };
  in
    nixpkgs.lib.mapAttrs (
      _: v: nixpkgs.callPackage v {inherit sources;}
    )
    packagePaths;
}
