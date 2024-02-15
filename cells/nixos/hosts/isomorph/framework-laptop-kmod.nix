{
  stdenv,
  lib,
  fetchFromGitHub,
  kernel,
  kmod,
}:
stdenv.mkDerivation rec {
  name = "framework-laptop-kmod";

  src = fetchFromGitHub {
    owner = "DHowett";
    repo = "framework-laptop-kmod";
    rev = "a9e8db9";
    sha256 = "0ic5k1jqdsz6plya93ysax8hn54vky6d449gmrnzja0sz73cwbq2";
  };

  hardeningDisable = ["pic" "format"]; # 1
  nativeBuildInputs = kernel.moduleBuildDependencies; # 2

  makeFlags = [
    # "KERNELRELEASE=${kernel.modDirVersion}"                                 # 3
    "KDIR=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build" # 4
    "INSTALL_MOD_PATH=$(out)" # 5
  ];

  meta = with lib; {
    description = " Kernel module to expose more Framework Laptop stuff ";
    homepage = "https://github.com/DHowett/framework-laptop-kmod";
    license = licenses.gpl2;
    maintainers = [maintainers.zyansheep];
    platforms = platforms.linux;
  };
}
