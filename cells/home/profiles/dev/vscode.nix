{
  inputs,
  cell,
}: {
  pkgs,
  lib,
  ...
}: let
  vs-exts = inputs.nix-vscode-extensions.extensions.vscode-marketplace;
in {
  programs.vscode = {
    enable = true;
    package = inputs.cells.common.overrides.vscode;
    # TODO split extensions based on active modules
    extensions = with vs-exts; [
      coolbear.systemd-unit-file
      davidanson.vscode-markdownlint
      christian-kohler.path-intellisense
      dbaeumer.vscode-eslint
      donjayamanne.githistory
      eamodio.gitlens
      editorconfig.editorconfig
      ivory-lab.jenkinsfile-support
      jq-syntax-highlighting.jq-syntax-highlighting
      maarti.jenkins-doc
      ms-azuretools.vscode-docker
      natqe.reload
      nicolasvuillamy.vscode-groovy-lint
      plorefice.devicetree
      redhat.vscode-commons
      redhat.vscode-xml
      redhat.vscode-yaml
      whi-tw.klipper-config-syntax
      xadillax.viml
      # yzane.markdown-pdf
      roscop.activefileinstatusbar
      pkief.material-icon-theme
      tamasfe.even-better-toml
      mads-hartmann.bash-ide-vscode
      ericadamski.carbon-now-sh
      # mkhl.direnv
      # remote
      ms-vscode-remote.remote-ssh
      ms-vscode-remote.remote-ssh-edit
    ];
    userSettings = builtins.fromJSON (builtins.readFile ./_files/vscode-settings.json);
  };
}
