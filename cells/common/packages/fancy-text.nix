{
  stdenvNoCC,
  esbuild,
  nodejs_24,
  writeShellApplication,
  wl-clipboard,
  wtype,
  coreutils,
}: let
  # The Vicinae extension's source, styles and tests; see its README.md.
  src = ../patches/fancy-text;
  extension = "share/vicinae/extensions/fancy-text";

  # What Enter runs: the clipboard + Shift+Insert paste emoji-picker uses.
  paste = writeShellApplication {
    name = "fancy-text-paste";
    # niri comes from PATH: the CLI has to match the running compositor.
    runtimeInputs = [wl-clipboard wtype coreutils];
    text = builtins.readFile "${src}/paste.sh";
  };
in
  stdenvNoCC.mkDerivation {
    pname = "fancy-text";
    version = "0.1.0";
    inherit src;
    nativeBuildInputs = [esbuild nodejs_24];
    # react and @vicinae/api come from Vicinae's extension runtime (as `vici build` leaves them).
    buildPhase = ''
      runHook preBuild
      esbuild src/fancy.tsx --bundle --format=cjs --platform=node --target=node22 --jsx=automatic \
        --external:react --external:react/jsx-runtime --external:@vicinae/api --minify --log-level=warning \
        '--define:FANCY_TEXT_PASTE="${paste}/bin/fancy-text-paste"' \
        --outfile=generate.js
      runHook postBuild
    '';
    doCheck = true;
    checkPhase = ''
      runHook preCheck
      HOME=$TMPDIR node --no-warnings --test test/*.test.ts
      runHook postCheck
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out/${extension}/assets
      cp package.json generate.js $out/${extension}/
      cp assets/icon.svg $out/${extension}/assets/
      runHook postInstall
    '';
    passthru = {inherit extension paste;};
    meta.description = "Fancy Unicode text styles (lingojam's Cool Text Fonts) for Vicinae";
  }
