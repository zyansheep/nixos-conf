{
  inputs,
  cell,
}: {
  common = _: {
    imports = [
      ./_common.nix
    ];

    sops.secrets = {
      root-password = {
        key = "root-password";
        sopsFile = ./sops/nixos-common.yaml;
        neededForUsers = true;
      };
    };
  };

  wifi = {
    sops.secrets = {
      xata-password-env = {
        key = "wireless290Env";
        sopsFile = ./sops/wifi.yaml;
      };
    };
  };

  minecraft-servers = _: {
    users.groups.minecraft-servers-backup = {};

    sops.secrets = {
      minecraft-restic-pw-file = {
        key = "mc-restic-pw";
        sopsFile = ./sops/minecraft-server.yaml;
        mode = "0440";
        group = "minecraft-servers-backup";
      };

      minecraft-restic-env-file = {
        key = "mc-restic-env";
        sopsFile = ./sops/minecraft-server.yaml;
        mode = "0440";
        group = "minecraft-servers-backup";
      };
    };
  };

  weather-kiosk = _: {
    sops.secrets.wk-openweathermap-api = {
      key = "openweathermap-api";
      sopsFile = ./sops/weather-kiosk.yaml;
    };

    sops.secrets.wk-influx-admin-pw = {
      key = "influx-admin-pw";
      sopsFile = ./sops/weather-kiosk.yaml;
    };

    sops.secrets.wk-influx-admin-token = {
      key = "influx-admin-token";
      sopsFile = ./sops/weather-kiosk.yaml;
    };
  };
}
