{
  lib,
  config,
  pkgs,
  ...
}: {
  environment.shellAliases = let
    ifSudo = lib.mkIf config.security.sudo.enable;
  in {
    # quick cd
    ".." = "cd ..";
    "..." = "cd ../..";
    "...." = "cd ../../..";
    "....." = "cd ../../../..";

    # git
    g = "git";

    # grep
    grep = "rg";
    gi = "grep -i";

    # internet ip
    myip = "dig +short myip.opendns.com @208.67.222.222 2>&1";

    # nix
    dbsu = "doas bud switch update";
    dbs = "does bud switch";
    n = "nix";
    np = "n profile";
    ni = "np install";
    nr = "np remove";
    ns = "n search --no-update-lock-file";
    nf = "n flake";
    nepl = "n repl '<nixpkgs>'";
    srch = "ns nixos";
    orch = "ns override";
    mn = ''
      manix "" | grep '^# ' | sed 's/^# \(.*\) (.*/\1/;s/ (.*//;s/^# //' | sk --preview="manix '{}'" | xargs manix
    '';

    # sudo
    sudo = "doas";

    # top
    top = "btm";

    # systemd
    ctl = "systemctl";
    stl = ifSudo "doas systemctl";
    utl = "systemctl --user";
    ut = "systemctl --user start";
    un = "systemctl --user stop";
    up = ifSudo "doas systemctl start";
    dn = ifSudo "doas systemctl stop";
    jtl = "journalctl";
  };
}
