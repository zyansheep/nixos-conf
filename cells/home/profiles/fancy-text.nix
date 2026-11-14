_: {pkgs, ...}: {
  # "Fancy Text" in Vicinae: what you type in 84 Unicode styles (cursive,
  # bubbles, upside down, vaporwave, …), Enter pastes it like the emoji picker.
  # Mod+Shift+; opens it (dotfiles/.config/niri/config.kdl). Built in
  # cells/common/packages/fancy-text.nix; see cells/common/patches/fancy-text/README.md.
  #
  # Vicinae watches ~/.local/share/vicinae/extensions only if it existed when
  # the server started (it does here, for jisho).
  xdg.dataFile."vicinae/extensions/fancy-text".source = "${pkgs.fancy-text}/${pkgs.fancy-text.extension}";
}
