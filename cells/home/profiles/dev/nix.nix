_: {pkgs, ...}: {
  programs.vscode.userSettings = {
    "nix.serverPath" = "nil";
    "nix.serverSettings" = {
      "nil" = {
        "formatting" = {
          "command" = ["alejandra"];
        };
      };
    };
    "nix.enableLanguageServer" = true;
    "[nix]" = {"editor.formatOnSave" = true;};
  };

  home.packages = with pkgs; [
    alejandra # Uses alejandra from overlay (latest)
    nil # Uses nil from overlay (latest)
  ];
}
