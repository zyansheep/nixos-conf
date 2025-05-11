{
  inputs,
  common,
}: {
  lib,
  config,
  pkgs,
  ...
}:
with lib; {
  environment.systemPackages = with pkgs; [
    (rWrapper.override {
      packages = with rPackages; [
        ggplot2
        reshape2
        dplyr
        xts
        ProbBayes
        tidyr
      ];
    })
  ];
}
