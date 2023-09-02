# This file was generated by nvfetcher, please do not modify it manually.
{ fetchgit, fetchurl, fetchFromGitHub, dockerTools }:
{
  graalvm-ee-17-aarch64-linux = {
    pname = "graalvm-ee-17-aarch64-linux";
    version = "22.3.1";
    src = fetchTarball {
      url = "https://oca.opensource.oracle.com/gds/GRAALVM_EE_JAVA17_22_3_1/graalvm-ee-java17-linux-aarch64-22.3.1.tar.gz";
      sha256 = "sha256-UomOanK5WJmx58OvMCDr5JBPPVrQ3jMyJfQYWD2xeAY=";
    };
  };
  graalvm-ee-17-x86_64-darwin = {
    pname = "graalvm-ee-17-x86_64-darwin";
    version = "22.3.1";
    src = fetchTarball {
      url = "https://oca.opensource.oracle.com/gds/GRAALVM_EE_JAVA17_22_3_1/graalvm-ee-java17-darwin-amd64-22.3.1.tar.gz";
      sha256 = "sha256-2s2B6FYO8lvOj9uVl4gTiDcQDix9/KkFGWDeyqcu30c=";
    };
  };
  graalvm-ee-17-x86_64-linux = {
    pname = "graalvm-ee-17-x86_64-linux";
    version = "22.3.1";
    src = fetchTarball {
      url = "https://oca.opensource.oracle.com/gds/GRAALVM_EE_JAVA17_22_3_1/graalvm-ee-java17-linux-amd64-22.3.1.tar.gz";
      sha256 = "sha256-M0mQAchpPt09lQvZePyaIQ86Hw1QZvghY7FTEeanRTc=";
    };
  };
}
