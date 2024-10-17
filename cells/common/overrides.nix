{
  inputs,
  cell,
}: let
  latest = import inputs.latest {
    inherit (inputs.nixpkgs) system;
    config.allowUnfree = true;
  };
in {
  inherit
    (latest)
    firefox
    vscodium
    alejandra
    nil
    nixpkgs-fmt
    statix
    nix
    cachix
    nix-index
    #
    
    ffmpeg_5-full
    ;

  # nix-diff = inputs.nix-diff.packages.default;
  nvfetcher = inputs.nvfetcher.packages.default;
}
