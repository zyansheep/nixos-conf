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
    nil
    nixUnstable
    #
    
    ffmpeg_5-full
    ;

  nvfetcher = inputs.nvfetcher.packages.default;

  power-profiles-daemon = latest.power-profiles-daemon.overrideAttrs (prev: {
    # temp fix to use version with amd pstate patch
    version = "git";
    src = latest.fetchFromGitLab {
      domain = "gitlab.freedesktop.org";
      owner = "superm1";
      repo = "power-profiles-daemon";
      rev = "e94256aaf6b9a9ef23686bef8c46c83e6442f121";
      sha256 = "sha256-TL9PDYTQQEc8m/VtxdtH/Z9W/Oq56TMojMz4sHXULHI=";
    };
  });
}
