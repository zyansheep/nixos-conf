# The `t3` CLI from the Z3 Code fork's releases (a self-contained Bun executable). Bump with z3code.nix.
# The fork ships only linux-x64.
{ lib, stdenv, stdenvNoCC, fetchurl, makeWrapper, autoPatchelfHook, vulkan-loader, codex }:
let
  version = "0.1.8-nightly.20261108.4380-z3.1";
  platform = "linux-x64";
in
stdenvNoCC.mkDerivation {
  pname = "t3-cli";
  inherit version;

  src = fetchurl {
    url = "https://github.com/zyansheep/t3code/releases/download/v${version}/t3-${version}-${platform}.tar.gz";
    hash = "sha256-1IUcZmXyX+w+9WmC4I9Bus5jr3rHyvArE0dP6QgtQvw=";
  };
  sourceRoot = "t3-${version}-${platform}";

  nativeBuildInputs = [ makeWrapper autoPatchelfHook ];
  # 0.1.7 also ships musl prebuilds of native modules (ffi-rs); node loads the glibc ones, so musl stays unlinked.
  autoPatchelfIgnoreMissingDeps = [ "libc.musl-x86_64.so.1" ];
  # The nightly CLI bundles a Vulkan build of its voice transcription backend (transcribe-cpp).
  buildInputs = [ stdenv.cc.cc.lib vulkan-loader ];

  dontConfigure = true;
  dontBuild = true;
  dontStrip = true; # Bun appends the bundled app to the executable

  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/t3 $out/bin
    cp -r . $out/lib/t3
    chmod +x $out/lib/t3/t3
    makeWrapper $out/lib/t3/t3 $out/bin/t3 --prefix PATH : ${lib.makeBinPath [ codex ]}
    runHook postInstall
  '';

  meta = {
    description = "Z3 Code CLI (zyansheep/t3code fork of T3 Code)";
    homepage = "https://github.com/zyansheep/t3code";
    license = lib.licenses.mit;
    mainProgram = "t3";
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    platforms = [ "x86_64-linux" ];
  };
}
