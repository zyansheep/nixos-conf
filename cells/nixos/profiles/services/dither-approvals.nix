# dither-approvals: ask the user from a service and get back an answer their phone (or this laptop's fingerprint
# reader) signed. The contract is dither-chat's docs/approvals.md; the hub is dither-chat on functor
# (https://chat.tn.zyancraft.net), the devices are paired in dither-accounts (Approvers on its page).
#
# - `dither-ask` (the dither-chat flake input) on PATH, with DITHER_ASK_URL, DITHER_ASK_TOKEN_FILE and DITHER_ASK_KEYS
#   in every session. The token (~/.config/dither-ask/token, 0600, not in the store) names this host as the asker; it
#   is made on functor with `bun packages/hub/src/cli.ts asker add <host> --kinds alert,deploy,rebuild --out F` in the
#   deployed hub's directory. A token can only ask: the answers that matter are signed by a device.
# - /etc/dither-approvals/keys: the devices whose signatures count (dither-approvals.keys). A pushed commit can't add a
#   device to a running host: the file comes from the system that runs. Add a line from `dither-ask keys` only after
#   comparing its fingerprint with the one the device itself shows.
# - nixos-deploy (alerts and questions after a failed deploy or a blocking local build) and nrb-request (update-manager
#   ask-rebuild) ask through it.
# - dither-approvals.review.enable (functor): the review watcher and merge bot for nixos-conf. Agents push
#   `review/<name>`; once nixos-ci built it, the user gets a merge request, and Approve makes a merge commit carrying
#   the signed approval, signed by the bot's key (~/.local/share/dither-review/bot_ed25519, in git-allowed-signers).
#   `dither-ask review status <name> --repo nixos-conf` says what became of a branch.
# - dither-approvals.deskSigner.enable (isomorph): the laptop as an approver device (dither-accounts apps/desk-signer),
#   its own system user, signing only after fprintd matches the user's finger. Pair it once:
#   `sudo desk-signer-pair '<dither-approver://pair?...>'` (the link from Approvers -> Pair a phone), then
#   `sudo systemctl start desk-signer`, then add its approval key to dither-approvals.keys.
{
  inputs,
  common,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.dither-approvals;
  user = "zyansheep";
  home = "/home/${user}";
  dither-ask = inputs.dither-chat.packages.${pkgs.stdenv.hostPlatform.system}.dither-ask;
  keysFile = pkgs.writeText "dither-approvals-keys" ''
    # device-id  kind  approval-key (SPKI, base64)  fingerprint  name
    ${lib.concatStringsSep "\n" cfg.keys}
  '';

  # apps/desk-signer and the one workspace package it imports, laid out so bun resolves @dither-accounts/approval
  # without an install (it has no npm dependencies).
  deskSigner = pkgs.runCommand "desk-signer" {nativeBuildInputs = [pkgs.makeWrapper];} ''
    mkdir -p $out/lib/desk-signer/node_modules/@dither-accounts $out/bin
    cp -r ${inputs.dither-accounts}/apps/desk-signer/. $out/lib/desk-signer/
    cp -r ${inputs.dither-accounts}/packages/approval $out/lib/desk-signer/node_modules/@dither-accounts/approval
    makeWrapper ${pkgs.bun}/bin/bun $out/bin/desk-signer --add-flags $out/lib/desk-signer/src/main.ts \
      --prefix PATH : ${lib.makeBinPath [pkgs.fprintd]}
  '';
  signerState = "/var/lib/desk-signer";
  deskSignerPair = pkgs.writeShellScriptBin "desk-signer-pair" ''
    [ "$(id -u)" = 0 ] || { echo "run it with sudo: sudo desk-signer-pair '<pairing link>' [--name NAME]" >&2; exit 2; }
    name=(--name ${config.networking.hostName})
    for a in "$@"; do [ "$a" = --name ] && name=(); done
    exec ${pkgs.util-linux}/bin/runuser -u desk-signer -- env DESK_SIGNER_STATE=${signerState} HOME=${signerState} \
      ${deskSigner}/bin/desk-signer pair "$@" "''${name[@]}"
  '';

  reviewConfig = pkgs.writeText "dither-review.json" (builtins.toJSON {
    hub = cfg.url;
    tokenFile = "${home}/.config/dither-review/token";
    keys = "/etc/dither-approvals/keys";
    botKey = "${home}/.local/share/dither-review/bot_ed25519";
    botName = "dither review bot";
    botEmail = "review-bot@functor";
    repos = [
      {
        name = "nixos-conf";
        remote = "https://github.com/zyansheep/nixos-conf.git";
        target = "main";
        # "<host> ok <path>" / "<host> FAILED" per host once nixos-ci built the commit; nothing (not yet) before.
        ci = {
          kind = "command";
          command = ["/run/current-system/sw/bin/nixos-ci" "results" "{rev}"];
        };
      }
    ];
  });
in {
  options.dither-approvals = {
    url = lib.mkOption {
      type = lib.types.str;
      default = "https://chat.tn.zyancraft.net";
      description = "dither-chat's hub, where approvals are asked.";
    };
    tokenFile = lib.mkOption {
      type = lib.types.str;
      default = "${home}/.config/dither-ask/token";
      description = "This host's asker token (not in the store).";
    };
    keys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      description = "Lines of /etc/dither-approvals/keys (as `dither-ask keys` prints them): the devices whose signed answers count.";
    };
    package = lib.mkOption {
      type = lib.types.package;
      default = dither-ask;
      readOnly = true;
    };
    review.enable = lib.mkEnableOption "the review watcher and merge bot for nixos-conf";
    deskSigner.enable = lib.mkEnableOption "this machine's fingerprint reader as an approver device";
  };

  config = lib.mkMerge [
    {
      # From `dither-ask keys` on 2026-11-20; the fingerprint is the one the app shows for this manager.
      dither-approvals.keys = [
        "dev_YuS4etBYW8HSbgMF phone MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEtD5mOhI7K/a0Plue7psE7OxUHTlDbB3wTYD4EzOdxEXuP7dsfafbqQWJRdKZQdlN/yMFmX2k959pa3NqPy687Q== 5e:ce:6b:1d:d8:9b:1e:44 Pixel 8a"
      ];

      environment.systemPackages = [dither-ask];
      environment.etc."dither-approvals/keys".source = keysFile;
      environment.sessionVariables = {
        DITHER_ASK_URL = cfg.url;
        DITHER_ASK_TOKEN_FILE = cfg.tokenFile;
        DITHER_ASK_KEYS = "/etc/dither-approvals/keys";
      };
    }

    (lib.mkIf cfg.review.enable {
      systemd.user.services.dither-review = {
        description = "dither review watcher: asks to merge nixos-conf's review/* branches once nixos-ci built them";
        wantedBy = ["default.target"];
        after = ["network-online.target"];
        wants = ["network-online.target"];
        unitConfig.ConditionUser = user;
        # git pushes with the user's credential helper (gh).
        path = [dither-ask pkgs.git pkgs.openssh pkgs.gh];
        serviceConfig = {
          ExecStart = "${dither-ask}/bin/dither-ask review watch --config ${reviewConfig}";
          Restart = "always";
          RestartSec = 60;
        };
      };
    })

    (lib.mkIf cfg.deskSigner.enable {
      services.fprintd.enable = true;
      users.users.desk-signer = {
        isSystemUser = true;
        group = "desk-signer";
        home = signerState;
      };
      users.groups.desk-signer = {};
      systemd.tmpfiles.rules = ["d ${signerState} 0700 desk-signer desk-signer -"];
      environment.systemPackages = [deskSignerPair];

      # fprintd asks polkit before it verifies another user's finger; only the signer may, and only these two actions.
      security.polkit.extraConfig = ''
        polkit.addRule(function (action, subject) {
          if (subject.user == "desk-signer" &&
              (action.id == "net.reactivated.fprint.device.verify" ||
               action.id == "net.reactivated.fprint.device.setusername"))
            return polkit.Result.YES;
        });
      '';

      systemd.services.desk-signer = {
        description = "desk-signer: this laptop's fingerprint reader as a dither-accounts approver device";
        wantedBy = ["multi-user.target"];
        after = ["network-online.target"];
        wants = ["network-online.target"];
        # Until it is paired (desk-signer-pair), there is nothing to run.
        unitConfig.ConditionPathExists = "${signerState}/device.json";
        environment = {
          DESK_SIGNER_STATE = signerState;
          DESK_SIGNER_USER = user;
          HOME = signerState;
        };
        serviceConfig = {
          User = "desk-signer";
          Group = "desk-signer";
          ExecStart = "${deskSigner}/bin/desk-signer run";
          StateDirectory = "desk-signer";
          StateDirectoryMode = "0700";
          Restart = "on-failure";
          RestartSec = 5;
          RestartPreventExitStatus = 2; # not paired: don't loop
          ProtectHome = true;
          PrivateTmp = true;
          NoNewPrivileges = true;
          ProtectSystem = "strict";
        };
      };
    })
  ];
}
