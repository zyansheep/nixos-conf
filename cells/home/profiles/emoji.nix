{inputs, ...}: {config, ...}: let
  kaomoji = "cells/home/profiles/_files/kaomoji.txt";
in {
  # `emoji-picker` — emoji, kaomoji and symbols in a Vicinae dmenu list,
  # pasted via the clipboard; Ctrl+E / Ctrl+Shift+N / Ctrl+Shift+K in it edit
  # the kaomoji. Code: github:zyansheep/kaomoji-picker (its README has the keys).
  # Its `vicinae dmenu --action` patch is applied in
  # cells/nixos/profiles/graphics/niri.nix.
  #
  # Bound to Mod+Semicolon in dotfiles/.config/niri/config.kdl.
  imports = [inputs.kaomoji-picker.homeManagerModules.default];

  # The kaomoji are ours and live here: the extension edits this checkout's
  # copy, the picker reads it live, and the next rebuild bakes it in.
  programs.emoji-picker = {
    enable = true;
    kaomoji = ./_files/kaomoji.txt;
    liveKaomoji = "${config.home.homeDirectory}/nixos-conf/${kaomoji}";
  };
}
