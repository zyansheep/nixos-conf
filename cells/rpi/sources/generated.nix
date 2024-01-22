# This file was generated by nvfetcher, please do not modify it manually.
{ fetchgit, fetchurl, fetchFromGitHub, dockerTools }:
{
  libcamera = {
    pname = "libcamera";
    version = "563cd78e1c9858769f7e4cc2628e2515836fd6e7";
    src = fetchFromGitHub {
      owner = "raspberrypi";
      repo = "libcamera";
      rev = "563cd78e1c9858769f7e4cc2628e2515836fd6e7";
      fetchSubmodules = false;
      sha256 = "sha256-T5MBTTYaDfaWEo/czTE822e5ZXQmcJ9pd+RWNoM4sBs=";
    };
    date = "2023-11-22";
  };
  libcamera-apps = {
    pname = "libcamera-apps";
    version = "9d8138994c86fd0a98c0ec5c6573197f111c051c";
    src = fetchFromGitHub {
      owner = "raspberrypi";
      repo = "libcamera-apps";
      rev = "9d8138994c86fd0a98c0ec5c6573197f111c051c";
      fetchSubmodules = false;
      sha256 = "sha256-PmRab8bKZ4XPu0aVoTIFLg5H3uso9aKyEHIY7ti8q7Q=";
    };
    date = "2024-01-15";
  };
  mediamtx = {
    pname = "mediamtx";
    version = "7b9617f2e73df0bebe360b23714b1acebf2c5183";
    src = fetchFromGitHub {
      owner = "bluenviron";
      repo = "mediamtx";
      rev = "7b9617f2e73df0bebe360b23714b1acebf2c5183";
      fetchSubmodules = false;
      sha256 = "sha256-DqmJaUd5tw/k+qVkrXp/9J2PDxUkC6pJ8KDkI66LiyQ=";
    };
    date = "2024-01-18";
  };
}
