_: {pkgs, ...}: let
  cldr = "${pkgs.cldr-annotations}/share/unicode/cldr/common";
  # What rofimoji's `math` set was built from; its TeX names become keywords.
  mathClass = pkgs.fetchurl {
    url = "https://www.unicode.org/Public/math/revision-15/MathClassEx-15.txt";
    hash = "sha256-WpcZyBOlNE4nKuWH/Ob8vpEhNYPWDi6oobTnTVcsggw=";
  };

  github = repo: rev: file: hash:
    pkgs.fetchurl {
      url = "https://raw.githubusercontent.com/${repo}/${rev}/${file}";
      inherit hash;
    };
  # Kaomoji collections, ~100k distinct faces between them, of which the
  # picker keeps the `kaomojiLimit` found in the most collections — the only
  # popularity signal there is (all of them: ≥3 collections; ~half: 2). They're
  # only fetched at build time, never vendored: several are GPL-3.0 or carry
  # no licence at all. Curated sets with good English tags come first, as they
  # win ties; tags from every collection that has a face are merged. `format`
  # picks the reader in ./_files/emoji-picker-data.py.
  kaomojiLimit = 10000;
  kaomoji = [
    # MIT; rofimoji's set
    { format = "w33ble"; src = github "w33ble/emoticon-data" "92b6211ec2a93e14052e0e572d697d4d06c71868" "emoticons.json" "sha256-YPnuPvYKhEWmh08RSOg4USWNpTMNcVOP2cbpoiMwfDE="; }
    # GPL-3.0; the best keywords
    { format = "all-in-one-clipboard"; src = github "NiffirgkcaJ/all-in-one-clipboard" "9c18f1c746caea6fad433ef5467e20c130da4c1e" "gnome-extensions/extension/assets/data/kaomoji/kaomojis.json" "sha256-OW7qDIqVkCxE8A/mLWlFuONaW0vywpC30pz8uyrjgHA="; }
    # MIT
    { format = "by-category"; src = github "Allaman/emoji.nvim" "372cb33e608941d2ddbdb60fc52eb78bfdf62ea2" "lua/data/kaomojis.json" "sha256-xMc6EmXvrVZGT4v3H2c+A0OBGop0XEeLhOMjdpTSBzc="; }
    # MIT
    {
      format = "vsedov";
      src = "${pkgs.fetchFromGitHub {
        owner = "vsedov";
        repo = "kaomoji_scrapper";
        rev = "b24315984160101063911f77720864a86d003c69";
        hash = "sha256-omFtviO5B08JFR275Wf9FF+2iKMuI4Xz6BZvgssSBI4=";
      }}/kaomoji/data/emoticons";
    }
    # MIT; what Raycast's kaomoji extension uses
    { format = "asciilib"; src = github "iansinnott/asciilib" "52b034b55a684251b4f0c4974d707b9de4d198f4" "lib.json" "sha256-d1/MxaoU9t//vWi2OAwzVMSjviFvj03CMHzMkZDhQ2w="; }
    # MIT
    { format = "by-category"; src = github "omnidan/node-kaomoji" "3cf155d007c6bde64b001311881a01ebe3564f76" "lib/kaomoji.json" "sha256-7ML0NBJR6u10m2qaWhXZ1dqSwQv6taGiaefy27t4ErQ="; }
    # GPL-3.0
    { format = "elisa-aleman"; src = github "elisa-aleman/Kaomoji_parsing" "e748a8bb8f0470e2b051ce58bdd5b54ec6c0f0e6" "Kaomoji.csv" "sha256-rZHcuImnxHprRk9T4nW3bVCLUTypGeOJlvH8PV9Llyg="; }
    # CC-BY-4.0, © Funovate (FontVibe)
    { format = "fontvibe"; src = github "Funovate/fontvibe-kaomoji" "7c5967824ddd3be6d1b28ce69ca01c99b124322b" "data/kaomoji.jsonl.gz" "sha256-E4srNMHcNL4NI9lCykLaMSYjWfvIFphpk4H+98SNZDA="; }
    # no licence
    { format = "codingstark"; src = github "codingstark-dev/kaomoji" "fa82cade98b091cc1a65d56c17481109ad320d62" "kaomoji.json" "sha256-ugJ8cQkBFLN4ZIrkUcl4Iii/Lr4ixfnGBq0po0vzAC4="; }
    # MIT; Japanese tags from here on
    { format = "kaomojikan"; src = github "kaomojikan/kaomoji-data" "60c92ea4e85279ad42ff525e9a40464ebeb1003e" "kaomoji.json" "sha256-InIeGzHWD+5am9nybIY5q07+4ubAHOw/Z08ODR066is="; }
    # MIT
    { format = "azookey"; src = github "ensan-hcl/azooKey" "b50db4aec1069a8d2341f70415da3bdc61f1fce6" "MainApp/DataSet/kaomoji_dict.tsv" "sha256-OW9HELADbUOJKI+8Mydyue/3ve6W209UCAL2hvVPvkw="; }
    # MIT
    { format = "kaomoji-json"; src = github "6/kaomoji-json" "ed898846a523f9ee122fe3979993b7dd92fc3512" "kao-utf8.json" "sha256-lKC82ChLDRJRS4OuaKtJLzfon225fJHZJ1v9sI7CbsU="; }
    # MIT; mostly romaji tags
    { format = "kaomojiya"; src = github "kaomojiya-collection/kaomoji-collection" "2ca1b394345a437bbec9e9b9445d1dd75ae3f7d4" "kaomoji.json" "sha256-c3Pl2r0E/ckc+0Uq9vZZUPppU6YTyATLzXkNPtTvc40="; }
    # no licence; ~50k faces, the biggest and noisiest
    { format = "ekohrt"; src = github "ekohrt/emoticon_kaomoji_dataset" "7d00fbba249adc1770025bcca56c74b42ad69ac1" "emoticon_dict.json" "sha256-c4UTgXnfX7UlIqrI0slZnWf1o0o9pLOhzYjkY7D2kF8="; }
  ];

  # `value<TAB>display` lines; see ./_files/emoji-picker-data.py.
  pickerData = pkgs.runCommand "emoji-picker-data" {nativeBuildInputs = [pkgs.python3];} ''
    python3 ${./_files/emoji-picker-data.py} \
      --emoji-test ${pkgs.unicode-emoji}/share/unicode/emoji/emoji-test.txt \
      --cldr ${cldr}/annotations/en.xml \
      --cldr ${cldr}/annotationsDerived/en.xml \
      ${toString (map (k: "--kaomoji ${k.format}=${k.src}") kaomoji)} \
      --kaomoji-limit ${toString kaomojiLimit} \
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
