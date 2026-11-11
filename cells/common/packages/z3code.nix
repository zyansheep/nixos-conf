# Z3 Code: the desktop app from the T3 Code fork's releases (zyansheep/t3code, branch `z3`: an upstream nightly
# release plus local patches). Bump `version` and `hash` together with t3-cli.nix.
#
# Built from the same nixpkgs as the system (the common-packages overlay), so the AppImage's FHS env has the
# glibc that /run/opengl-driver's mesa was linked against. Packaged from a separately locked flake
# (~/Repos/t3code-nix) it fell behind: mesa needed GLIBC_2.43, the GPU process failed to load radeonsi, and
# Electron silently fell back to software rendering (`--use-gl=disabled` on the gpu-process), so the UI crawled.
{ lib, appimageTools, fetchurl, makeWrapper, codex }:
let
  pname = "t3code"; # binary, desktop file, keyring entry and profile dir all keep the upstream name
  version = "0.1.9-nightly.20261110.4491-z3.1";
  src = fetchurl {
    url = "https://github.com/zyansheep/t3code/releases/download/v${version}/Z3-Code-${version}-x86_64.AppImage";
    hash = "sha256-xJog8l4006f9lqG34C4q1eRvsNmKK5D1krUYGd+A3Po=";
  };
  contents = appimageTools.extract { inherit pname version src; };
in
appimageTools.wrapType2 {
  inherit pname version src;
  nativeBuildInputs = [ makeWrapper ];

  extraInstallCommands = ''
    mkdir -p $out/share
    cp -r ${contents}/usr/share/* $out/share/
    install -Dm444 ${contents}/t3code.desktop $out/share/applications/t3code.desktop
    sed -i \
      -e 's|^Exec=AppRun|Exec=t3code|' \
      -e 's|^TryExec=AppRun|TryExec=t3code|' \
      -e 's|^StartupWMClass=.*$|StartupWMClass=t3-code-desktop|' \
      $out/share/applications/t3code.desktop

    # T3CODE_DESKTOP_LAUNCHER_PATH: what the t3code:// handler entry Execs (else the raw extracted binary).
    # T3CODE_ANNOTATION_EVENTS_URL: chat <abbr> popover interactions go to Japanese Weave's collector on functor.
    # T3CODE_PORT: the port scan probes "::", which t3code-tailnet6's socat on [fd7a:115c:a1e0::2]:3773 makes look
    # busy, so without the pin Z3 lands on 3774 and functor's environment link (forwarded to :3773) is refused.
    wrapProgram $out/bin/t3code \
      --set CHROME_DESKTOP t3code.desktop \
      --set-default T3CODE_DESKTOP_LAUNCHER_PATH $out/bin/t3code \
      --set-default T3CODE_ANNOTATION_EVENTS_URL http://functor:7853/v1/interactions \
      --set-default T3CODE_PORT 3773 \
      --prefix XDG_DATA_DIRS : $out/share \
      --prefix PATH : ${lib.makeBinPath [ codex ]}
  '';

  meta = {
    description = "Z3 Code, the zyansheep/t3code fork of T3 Code (desktop app)";
    homepage = "https://github.com/zyansheep/t3code";
    license = lib.licenses.mit;
    mainProgram = "t3code";
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    platforms = [ "x86_64-linux" ];
  };
}
