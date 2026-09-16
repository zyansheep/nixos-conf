{ stdenvNoCC, python3, sources ? null }:
stdenvNoCC.mkDerivation {
  pname = "waybar-monitor";
  version = "0.1.0";
  src = ../patches/system-monitor;
  nativeBuildInputs = [ python3 ];
  dontBuild = true;
  doCheck = true;
  checkPhase = "python3 -m unittest discover -s . -p 'test_*.py'";
  installPhase = ''
    install -Dm755 collector.py $out/bin/waybar-monitor
    patchShebangs $out/bin/waybar-monitor
  '';
}
