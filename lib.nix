{haumea}: {
  # Import every package file in a directory with callPackage.
  importPackages = {
    nixpkgs,
    packages,
  }:
    nixpkgs.lib.mapAttrs (_: v: nixpkgs.callPackage v {}) (haumea.lib.load {
      src = packages;
      loader = haumea.lib.loaders.path;
    });
}
