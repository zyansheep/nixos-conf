# This file was generated by nvfetcher, please do not modify it manually.
{ fetchgit, fetchurl, fetchFromGitHub, dockerTools }:
{
  camera-streamer = {
    pname = "camera-streamer";
    version = "v0.2.8";
    src = fetchFromGitHub {
      owner = "ayufan";
      repo = "camera-streamer";
      rev = "v0.2.8";
      fetchSubmodules = true;
      sha256 = "sha256-8vV8BMFoDeh22I1/qxk6zttJROaD/lrThBxXHZSPpT4=";
    };
  };
  katapult = {
    pname = "katapult";
    version = "3e23332eb188244e88f5ff60aecf077cd32bcd0c";
    src = fetchFromGitHub {
      owner = "Arksine";
      repo = "katapult";
      rev = "3e23332eb188244e88f5ff60aecf077cd32bcd0c";
      fetchSubmodules = false;
      sha256 = "sha256-xFelN6fzcsdt60Ifo/qxbkj691rUfvnZzmg3nrYCAYI=";
    };
    date = "2024-02-10";
  };
  klipper = {
    pname = "klipper";
    version = "bfb71bc2dc63f2911a11ebf580f82b1e8b2706c4";
    src = fetchFromGitHub {
      owner = "Klipper3d";
      repo = "klipper";
      rev = "bfb71bc2dc63f2911a11ebf580f82b1e8b2706c4";
      fetchSubmodules = false;
      sha256 = "sha256-djF1IOcMCBcsmVV0hgn6QMwDVClxSSithgiRvss9KQc=";
    };
    date = "2024-03-15";
  };
  klipper-ercf = {
    pname = "klipper-ercf";
    version = "fb9a6c7daaf881ab507368386e733a800b26f595";
    src = fetchFromGitHub {
      owner = "EtteGit";
      repo = "EnragedRabbitProject";
      rev = "fb9a6c7daaf881ab507368386e733a800b26f595";
      fetchSubmodules = false;
      sha256 = "sha256-Gwi0HiCIlTBQkHlFnmG0sZWB2xA1y5maJxF7VdfQMxU=";
    };
    date = "2022-09-02";
  };
  klipper-gcode_shell_command = {
    pname = "klipper-gcode_shell_command";
    version = "b6c6edb6227a0828abe4ccd15640f895671ffd23";
    src = fetchFromGitHub {
      owner = "dw-0";
      repo = "kiauh";
      rev = "b6c6edb6227a0828abe4ccd15640f895671ffd23";
      fetchSubmodules = false;
      sha256 = "sha256-VoyyVq7Yp89r3xMa7emvVT3j+gExJCEOlJp+sJjqqog=";
    };
    date = "2024-02-24";
  };
  klipper-happy-hare = {
    pname = "klipper-happy-hare";
    version = "f9cf5a2abcfab76adbb1cd88b567dfffb110eec5";
    src = fetchFromGitHub {
      owner = "moggieuk";
      repo = "Happy-Hare";
      rev = "f9cf5a2abcfab76adbb1cd88b567dfffb110eec5";
      fetchSubmodules = false;
      sha256 = "sha256-/VtFUkOZbmivoutwASWzRQaTUazXydhC+kJ6C0UkSl8=";
    };
    date = "2024-03-15";
  };
  klipper-kamp = {
    pname = "klipper-kamp";
    version = "v1.1.2";
    src = fetchFromGitHub {
      owner = "kyleisah";
      repo = "Klipper-Adaptive-Meshing-Purging";
      rev = "v1.1.2";
      fetchSubmodules = false;
      sha256 = "sha256-anBGjLtYlyrxeNVy1TEMcAGTVUFrGClLuoJZuo3xlDM=";
    };
  };
  klipper-klicky-probe = {
    pname = "klipper-klicky-probe";
    version = "7cee01de80e71ef78cf1b5cedab5d0239a0d6577";
    src = fetchFromGitHub {
      owner = "truelecter";
      repo = "Klicky-Probe";
      rev = "7cee01de80e71ef78cf1b5cedab5d0239a0d6577";
      fetchSubmodules = false;
      sha256 = "sha256-NciHhw3n8CDTfn/kBMNqYC/+7dcdqicysOGaKZ/wxHk=";
    };
    date = "2024-03-08";
  };
  klipper-klippain-shaketune = {
    pname = "klipper-klippain-shaketune";
    version = "82b91c1b40e2a121136c483dd9acfb11b56b4350";
    src = fetchFromGitHub {
      owner = "Frix-x";
      repo = "klippain-shaketune";
      rev = "82b91c1b40e2a121136c483dd9acfb11b56b4350";
      fetchSubmodules = false;
      sha256 = "sha256-MQBUnNjtbJ7VI4orlGsbLNE8PYwY8GFm2sFG0kh9PZ0=";
    };
    date = "2024-03-15";
  };
  klipper-led_effect = {
    pname = "klipper-led_effect";
    version = "c735fe52e1070632919f125513a0552e03739a5f";
    src = fetchFromGitHub {
      owner = "julianschill";
      repo = "klipper-led_effect";
      rev = "c735fe52e1070632919f125513a0552e03739a5f";
      fetchSubmodules = false;
      sha256 = "sha256-68l6iEnv+B8UewHFHA3dmiXFydKUUdCTtq0RsAgfN9Y=";
    };
    date = "2023-10-14";
  };
  klipper-screen = {
    pname = "klipper-screen";
    version = "16f967a86c7a5145a9fdd5a0f7e76682e1b3cf75";
    src = fetchFromGitHub {
      owner = "jordanruthe";
      repo = "KlipperScreen";
      rev = "16f967a86c7a5145a9fdd5a0f7e76682e1b3cf75";
      fetchSubmodules = false;
      sha256 = "sha256-RVu9UTYnNe1lnTx5UUedwSZwerKR/aUFCUFohi/ntYc=";
    };
    date = "2024-03-15";
  };
  klipper-z-calibration = {
    pname = "klipper-z-calibration";
    version = "v1.0.2";
    src = fetchFromGitHub {
      owner = "protoloft";
      repo = "klipper_z_calibration";
      rev = "v1.0.2";
      fetchSubmodules = false;
      sha256 = "sha256-QDsr09aIP09pI2r18atTRvbbMBS0rbjeiWOSnp9nRUk=";
    };
  };
  libdatachannel0_17 = {
    pname = "libdatachannel0_17";
    version = "v0.17.10";
    src = fetchFromGitHub {
      owner = "paullouisageneau";
      repo = "libdatachannel";
      rev = "v0.17.10";
      fetchSubmodules = false;
      sha256 = "sha256-3f84GxAgQiObe+DYuTQABvK+RTihKKFKaf48lscUex4=";
    };
  };
  libjuice = {
    pname = "libjuice";
    version = "v1.0.4";
    src = fetchFromGitHub {
      owner = "paullouisageneau";
      repo = "libjuice";
      rev = "v1.0.4";
      fetchSubmodules = false;
      sha256 = "sha256-LAqi5F6okhGj0LyJasPKRkUz6InlM6rbYN+1sX1N4Qo=";
    };
  };
  live555 = {
    pname = "live555";
    version = "2020.03.06";
    src = fetchTarball {
      url = "https://download.videolan.org/pub/contrib/live555/live.2020.03.06.tar.gz";
      sha256 = "sha256-UxunbrJC6UVlwexso/cuH9m+mUSf7WEPMLincUrqDWE=";
    };
  };
  mainsail = {
    pname = "mainsail";
    version = "972266d2268c385f690dbbf0f894ab7298e7e071";
    src = fetchFromGitHub {
      owner = "mainsail-crew";
      repo = "mainsail";
      rev = "972266d2268c385f690dbbf0f894ab7298e7e071";
      fetchSubmodules = false;
      sha256 = "sha256-2EGCcsfCFUfJ/95AxUTcG96utGB42ypyQ6uzJ/G7c9g=";
    };
    date = "2024-03-13";
  };
  mobileraker-companion = {
    pname = "mobileraker-companion";
    version = "b745f5ce39459b03388738ebb1f5cd0df6969ab3";
    src = fetchFromGitHub {
      owner = "Clon1998";
      repo = "mobileraker_companion";
      rev = "b745f5ce39459b03388738ebb1f5cd0df6969ab3";
      fetchSubmodules = false;
      sha256 = "sha256-PSHZGqfOs0YvwpqAlfo180uv3ztZ42xC3HOCb3cV3ns=";
    };
    date = "2024-03-02";
  };
  moonraker = {
    pname = "moonraker";
    version = "6b1b8c51025c6a8650b4cecfefb47f0ff1d4415a";
    src = fetchFromGitHub {
      owner = "Arksine";
      repo = "moonraker";
      rev = "6b1b8c51025c6a8650b4cecfefb47f0ff1d4415a";
      fetchSubmodules = false;
      sha256 = "sha256-gjRgqDL7mD1texpMKfaxxyUpZgKwZjX28WZSQX2XAm4=";
    };
    date = "2024-03-05";
  };
  python-networkmanager = {
    pname = "python-networkmanager";
    version = "2.2";
    src = fetchurl {
      url = "https://pypi.org/packages/source/p/python-networkmanager/python-networkmanager-2.2.tar.gz";
      sha256 = "sha256-3m65IdlKunVJ9CjtKzqkgqXVQ+y2lly6oPu1VasxudU=";
    };
  };
}
