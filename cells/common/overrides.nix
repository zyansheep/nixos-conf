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
    vscode
    alejandra
    rnix-lsp
    nixUnstable
    #
    ffmpeg_5-full
    ;

  nvfetcher = inputs.nvfetcher.packages.default;
}
