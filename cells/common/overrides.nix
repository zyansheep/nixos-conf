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

  power-profiles-daemon = latest.power-profiles-daemon.overrideAttrs (prev: { # temp fix to use version with amd pstate patch
    version = "git";
    src = latest.fetchFromGitLab {
      domain = "gitlab.freedesktop.org";
      owner = "superm1";
      repo = "power-profiles-daemon";
      rev = "26fa4de0af579ad88636b58c77a81b520250f19a";
      sha256 = "sha256-SNG5ajiq5kcADqQrZtlVxNIAEwtcYlAUQIQF005t5Jo=";
    };
  });
}
