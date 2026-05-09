{
  inputs,
  common,
}: _: {
  services.sanoid = {
    enable = true;
    interval = "hourly";

    datasets = {
      "zpool/safe" = {
        hourly = 1;
        daily = 15;
        monthly = 12;
        yearly = 1;
        autoprune = true;
        autosnap = true;
        recursive = true;
      };
    };
  };
}
