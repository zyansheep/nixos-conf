{ lib, stdenvNoCC, fetchurl, runCommand, python3, esbuild, nodejs_24, translatelocally,
  writeShellApplication, wl-clipboard, jq, coreutils }:
let
  # The Vicinae extension's source, data builder and tests; see its README.md.
  src = ../patches/jisho;

  release = "3.6.2+20261019180022"; # jmdict-simplified: JMdict with Tatoeba examples, KANJIDIC2, KRADFILE.
  simplified = name: hash: fetchurl {
    url = "https://github.com/scriptin/jmdict-simplified/releases/download/${lib.escapeURL release}/${name}-${release}.json.tgz";
    inherit hash;
  };
  jmdict = simplified "jmdict-examples-eng" "sha256-ydWCthSiOembeeAZTISzC+4VabMW0fcro44dJmmdm+o=";
  kanjidic = simplified "kanjidic2-en" "sha256-HAglXo6qziyi1GiXl/drIcfbov2L2T/jlGjC+/F15v0=";
  kradfile = simplified "kradfile" "sha256-vEcb7tfIX2bBvY2pTXBXurI/c0X8BG2H59TlgiloXxk=";
  kanjivg = fetchurl {
    url = "https://github.com/KanjiVG/kanjivg/releases/download/r20260714/kanjivg-20260714-main.zip";
    hash = "sha256-td9s0rwkndSbgEHrmeOLqfjca56leWKoAIS0oxXRp/w=";
  };
  # JPDB's frequency ranks, as Yomitan dictionaries (Kuuuube/yomitan-dictionaries).
  jpdb = fetchurl {
    url = "https://raw.githubusercontent.com/Kuuuube/yomitan-dictionaries/d6fde809e3f26eb5aed6d41896f332179044998c/dictionaries/JPDB_v2.2_Frequency_Kana_2024-10-13.zip";
    hash = "sha256-T6BseEFV6g6glTdAuZ1CEpbHde4NA1z+H/ZaQNfT5oU=";
  };
  accents = fetchurl {
    url = "https://raw.githubusercontent.com/mifunetoshiro/kanjium/9ebca4589565c696e7c39e089e2e928ad65f678c/data/source_files/raw/accents.txt";
    hash = "sha256-i9DdEn2rMs7slMsDqxumtohY6nNCHfoXMa8vNz3rTyA=";
  };
  jlpt = lib.mapAttrsToList (level: hash: fetchurl {
    name = "jlpt-${level}.csv";
    url = "https://raw.githubusercontent.com/jamsinclair/open-anki-jlpt-decks/1ad66734417aca9dbcca6b2d5ee440cb13ab3ba0/src/${level}.csv";
    inherit hash;
  }) {
    n5 = "sha256-+Jq8hrORxPK1Ubv4kQsN5s1gCXOzqfjZg0dROSGSnr4=";
    n4 = "sha256-DoNfQKjSofGR16pJkHYAJ1b6CZuVY0dYN29DxzeT6fY=";
    n3 = "sha256-ugcVcdNE5gsOH9Ec0umKaqoEUVNhZTykUUcSlGBTEpc=";
    n2 = "sha256-LQ8d3WIiiBzZ/CynAdt0MAr5mz8fhNWsPBhBHCDwwFU=";
    n1 = "sha256-EgkRY2wBmJlVKqbXvWSwNuzvS+3+Jyp0T3VzXEaq5c0=";
  };

  database = runCommand "jisho-database" { nativeBuildInputs = [ python3 ]; } ''
    tar xzf ${jmdict}; tar xzf ${kanjidic}; tar xzf ${kradfile}
    mkdir $out
    python3 ${src}/build_db.py --jmdict jmdict-examples-eng-*.json --kanjidic kanjidic2-en-*.json \
      --kradfile kradfile-*.json --kanjivg ${kanjivg} --jpdb ${jpdb} --accents ${accents} \
      --jlpt ${lib.concatStringsSep " " jlpt} --out $out/jisho.sqlite3
  '';

  # Mozilla's Firefox Translations models (Bergamot), as translateLocally
  # finds them: $XDG_DATA_DIRS/translateLocally/models/<name>/. Their registry:
  # https://storage.googleapis.com/moz-fx-translations-data--303e-prod-translations-data/db/models.json
  # (ja-en: the desktop release, base; en-ja: the release, base-memory).
  model = pair: dir: files: let
    from = builtins.substring 0 2 pair;
    to = builtins.substring 2 2 pair;
    names = { ja = "Japanese"; en = "English"; };
    fetched = lib.mapAttrsToList (file: hash: fetchurl {
      url = "https://storage.googleapis.com/moz-fx-translations-data--303e-prod-translations-data/models/${from}-${to}/${dir}/exported/${file}.gz";
      inherit hash;
    }) files;
    vocabs = if files ? "vocab.${pair}.spm" then [ "vocab.${pair}.spm" "vocab.${pair}.spm" ]
      else [ "srcvocab.${pair}.spm" "trgvocab.${pair}.spm" ];
  in ''
    d=$out/share/translateLocally/models/${from}-${to}-base
    mkdir -p $d
    ${lib.concatMapStrings (f: "gunzip -c ${f} > $d/$(basename ${f.name} .gz)\n") fetched}
    cat > $d/config.intgemm8bitalpha.yml <<EOF
    relative-paths: true
    models: [model.${pair}.intgemm.alphas.bin]
    vocabs: [${lib.concatStringsSep ", " vocabs}]
    shortlist: [lex.50.50.${pair}.s2t.bin, false]
    beam-size: 1
    normalize: 1.0
    word-penalty: 0
    max-length-break: 128
    mini-batch-words: 1024
    workspace: 128
    max-length-factor: 2.0
    skip-cost: true
    cpu-threads: 0
    quiet: true
    quiet-translation: true
    gemm-precision: int8shiftAlphaAll
    alignment: soft
    EOF
    echo '{"shortName": "${from}-${to}-base", "modelName": "${names.${from}}-${names.${to}} (Firefox Translations)",
      "src": "${names.${from}}", "trg": "${names.${to}}", "srcTags": {"${from}": "${names.${from}}"},
      "trgTag": "${to}", "type": "base", "repository": "Mozilla", "version": 1, "API": 1}' > $d/model_info.json
  '';
  models = runCommand "jisho-translation-models" { } (
    model "jaen" "cjk_icu_base_U4VUAW3STh-bF0Sr-dX69g" {
      "model.jaen.intgemm.alphas.bin" = "sha256-SDya0Y6F/QqMyxWzBdr3kp7pOavWBv21Wkys+omlvh8=";
      "lex.50.50.jaen.s2t.bin" = "sha256-JmYXN0qUrdSQoRXwWUwN8Yu7aWyUoFazlt3FPlOT3Hs=";
      "vocab.jaen.spm" = "sha256-M5yN9CBfgeahku8vfRu+qSjBg6KweHhyPfiVzyPa7aw=";
    }
    + model "enja" "llmaat_finetune10M_qe8_f2_ApiGGQIwTKuF9i_k3n9Q2Q" {
      "model.enja.intgemm.alphas.bin" = "sha256-mfyPwSAYmTC+rayy7vSwSdB5SsYTLRJmNA5FfJVzA2g=";
      "lex.50.50.enja.s2t.bin" = "sha256-eWY4L3qboVmTxnchUmkh2JAcqg+K3Vbv8E5LsyfVfBE=";
      "srcvocab.enja.spm" = "sha256-Etj/yD/Au2FJTZVwH2opi7rVyZmgQ9Skihwb1h4JIWc=";
      "trgvocab.enja.spm" = "sha256-vn/sI0tfyUYWJ27mzKafOoy/pnYv3yeN/GsR6vZpzEg=";
    });

  translator = "${translatelocally}/bin/translateLocally";
  extension = "share/vicinae/extensions/jisho";

  # Mod+Shift+' in niri: the selected text (or else the clipboard) in the dictionary.
  lookup = writeShellApplication {
    name = "jisho-lookup";
    # vicinae comes from PATH: the CLI has to match the resident server (niri.nix).
    runtimeInputs = [ wl-clipboard jq coreutils ];
    text = ''
      text=$(wl-paste --primary --no-newline 2>/dev/null || true)
      [ -n "$text" ] || text=$(wl-paste --no-newline --type text 2>/dev/null || true)
      query=$(printf '%s' "$text" | tr '\n\t\r' '   ' | head -c 4000 | jq -sRr @uri)
      exec vicinae deeplink "vicinae://launch/@zyansheep/jisho/search?fallbackText=$query"
    '';
  };
in
stdenvNoCC.mkDerivation {
  pname = "jisho";
  version = "0.1.0";
  inherit src;
  nativeBuildInputs = [ esbuild nodejs_24 ];
  # react and @vicinae/api come from Vicinae's extension runtime (as `vici build` leaves them).
  buildPhase = ''
    runHook preBuild
    esbuild src/search.tsx --bundle --format=cjs --platform=node --target=node22 --jsx=automatic \
      --external:react --external:react/jsx-runtime --external:@vicinae/api --minify --log-level=warning \
      '--define:JISHO_TRANSLATOR="${translator}"' '--define:JISHO_MODELS="${models}/share"' \
      --outfile=search.js
    runHook postBuild
  '';
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export JISHO_DB=${database}/jisho.sqlite3 JISHO_TRANSLATOR=${translator} JISHO_MODELS=${models}/share HOME=$TMPDIR
    node --no-warnings --test test/*.test.ts
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out/${extension}/assets $out/bin
    cp package.json search.js $out/${extension}/
    cp assets/icon.svg $out/${extension}/assets/
    ln -s ${database}/jisho.sqlite3 $out/${extension}/assets/jisho.sqlite3
    ln -s ${lookup}/bin/jisho-lookup $out/bin/jisho-lookup
    runHook postInstall
  '';
  passthru = { inherit database models extension; };
  meta.description = "Japanese dictionary and offline translator for Vicinae";
}
