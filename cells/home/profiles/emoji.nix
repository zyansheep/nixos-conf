_: {pkgs, ...}: let
  cldr = "${pkgs.cldr-annotations}/share/unicode/cldr/common";
  # What rofimoji's `math` set was built from; its TeX names become keywords.
  mathClass = pkgs.fetchurl {
    url = "https://www.unicode.org/Public/math/revision-15/MathClassEx-15.txt";
    hash = "sha256-WpcZyBOlNE4nKuWH/Ob8vpEhNYPWDi6oobTnTVcsggw=";
  };

  # `value<TAB>display` lines; see ./_files/emoji-picker-data.py.
  pickerData = pkgs.runCommand "emoji-picker-data" {nativeBuildInputs = [pkgs.python3];} ''
    python3 ${./_files/emoji-picker-data.py} \
      --emoji-test ${pkgs.unicode-emoji}/share/unicode/emoji/emoji-test.txt \
      --cldr ${cldr}/annotations/en.xml \
      --cldr ${cldr}/annotationsDerived/en.xml \
      --kaomoji ${./_files/kaomoji.txt} \
      --math-class ${mathClass} \
      --unicode-data ${pkgs.unicode-character-database}/share/unicode/UnicodeData.txt \
      > $out
  '';
in {
  # `emoji-picker` — emoji, kaomoji and math symbols in a Vicinae dmenu list,
  # inserted via the clipboard instead of synthesising keystrokes for the
  # glyph itself (which Electron/Firefox lose or garble); see
  # ./_files/emoji-picker.sh.
  #
  # Bound to Mod+Semicolon in dotfiles/.config/niri/config.kdl.
  home.packages = [
    (pkgs.writeShellApplication {
      name = "emoji-picker";
      # vicinae itself comes from PATH: the CLI has to match the resident
      # server, which is the patched build in cells/nixos/profiles/graphics/niri.nix.
      runtimeInputs = with pkgs; [wl-clipboard wtype coreutils gawk gnugrep];
      runtimeEnv.EMOJI_PICKER_DATA = pickerData;
      text = builtins.readFile ./_files/emoji-picker.sh;
    })
  ];
}
