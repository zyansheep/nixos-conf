_: {pkgs, ...}: {
  # "Japanese Dictionary" in Vicinae: words in English, Japanese or romaji,
  # kanji, conjugations and offline translation; Mod+' opens it and Mod+Shift+'
  # looks up the selection (dotfiles/.config/niri/config.kdl). The extension is
  # built in cells/common/packages/jisho.nix; see cells/common/patches/jisho/README.md.
  #
  # Vicinae finds extensions in ~/.local/share/vicinae/extensions, watching it
  # only if it existed when the server started: restart vicinae after adding one.
  xdg.dataFile."vicinae/extensions/jisho".source = "${pkgs.jisho}/${pkgs.jisho.extension}";
  home.packages = [pkgs.jisho]; # jisho-lookup
}
