{
  lib,
  config,
  ...
}: {
  environment.shellAliases = let
    ifSudo = lib.mkIf config.security.sudo.enable;
  in {
    # git
    g = "git";

    # grep
    grep = "rg";
    gi = "grep -i";

    # internet ip
    myip = "dig +short myip.opendns.com @208.67.222.222 2>&1";

    # nix
    n = "nix";
    np = "n profile"; # nix profile
    npi = "np install"; # nix profile install
    npr = "np remove"; # nix profile remove
    ns = "n search --no-update-lock-file";
    nsl = "ns l"; # search latest nixpkgs
    nf = "n flake";
    nrb = "git add . && nixos-rebuild --flake . --use-remote-sudo switch";
    nrbu = "nf update && nrb";

    # sudo
    sudo = "doas";

    # systemd
    ctl = "systemctl";
    stl = ifSudo "doas systemctl";
    utl = "systemctl --user";
    ut = "systemctl --user start";
    un = "systemctl --user stop";
    up = ifSudo "doas systemctl start";
    dn = ifSudo "doas systemctl stop";
    jtl = "journalctl";

    sc = "hx ~/.config/sway/config";
  };
}
