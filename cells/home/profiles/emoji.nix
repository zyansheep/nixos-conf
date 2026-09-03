_: {pkgs, ...}: {
  # `emoji-picker` — rofimoji wrapper that inserts via the clipboard instead of
  # synthesising keystrokes for the glyph itself.
  #
  # Straight `rofimoji` opens fine but its default `type` action loses or
  # mangles the emoji in Electron/Firefox windows, because wtype uploads a
  # custom keymap and presses it before those clients have re-read it. The
  # wrapper copies + Shift+Insert instead; see ./_files/emoji-picker.sh.
  #
  # Bound to Mod+Semicolon in dotfiles/.config/niri/config.kdl.
  home.packages = [
    (pkgs.writeShellApplication {
      name = "emoji-picker";
      runtimeInputs = with pkgs; [rofimoji wl-clipboard wtype coreutils];
      text = builtins.readFile ./_files/emoji-picker.sh;
    })
  ];
}
