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
    vscodium
    alejandra
    rnix-lsp
    nixUnstable
    #
    ffmpeg_5-full
    ;

  nvfetcher = inputs.nvfetcher.packages.default;
}
