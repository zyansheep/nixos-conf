_:
{ lib, pkgs, config, ... }: {
  xdg.enable = true;
  xdg.mimeApps = {
    enable = true;
    associations.added = {
      "audio/mp4" = "mpv.desktop";
      "inode/directory" = ["org.kde.dolphin.desktop" "codium.desktop" "org.kde.kate.desktop" "org.kde.gwenview.desktop"];
      "text/html" = "firefox.desktop";
      "x-scheme-handler/geo" = "qwant-maps-geo-handler.desktop";
      "x-scheme-handler/http" = ["floorp.desktop" "firefox-nightly.desktop" "firefox.desktop"];
      "x-scheme-handler/https" = ["floorp.desktop" "firefox-nightly.desktop" "firefox.desktop"];
      "x-scheme-handler/obsidian" = "obsidian.desktop";
      "x-scheme-handler/sgnl" = ["signal-desktop.desktop" "signal.desktop"];
      "x-scheme-handler/signalcaptcha" = ["signal-desktop.desktop" "signal.desktop"];
      "x-scheme-handler/tel" = "org.kde.kdeconnect.handler.desktop";
      "x-scheme-handler/tg" = "org.telegram.desktop.desktop";
      "x-scheme-handler/tonsite" = "org.telegram.desktop.desktop";
    };
    defaultApplications = {
      "application/epub+zip" = "org.pwmt.zathura-pdf-mupdf.desktop";
      "application/pdf" = "org.pwmt.zathura-pdf-mupdf.desktop";
      "audio/aac" = "mpv.desktop";
      "audio/flac" = "mpv.desktop";
      "audio/mp4" = "mpv.desktop";
      "audio/mpeg" = "mpv.desktop";
      "audio/ogg" = "mpv.desktop";
      "audio/opus" = "mpv.desktop";
      "audio/vnd.wave" = "mpv.desktop";
      "audio/wav" = "mpv.desktop";
      "audio/webm" = "mpv.desktop";
      "audio/x-aiff" = "mpv.desktop";
      "audio/x-flac" = "mpv.desktop";
      "audio/x-matroska" = "mpv.desktop";
      "audio/x-mp3" = "mpv.desktop";
      "audio/x-mpegurl" = "mpv.desktop";
      "audio/x-ms-wma" = "mpv.desktop";
      "audio/x-vorbis+ogg" = "mpv.desktop";
      "audio/x-wav" = "mpv.desktop";
      "inode/directory" = "org.kde.dolphin.desktop";
      "x-scheme-handler/chrome" = "firefox.desktop";
      "x-scheme-handler/geo" = "qwant-maps-geo-handler.desktop";
      "x-scheme-handler/http" = "floorp.desktop";
      "x-scheme-handler/https" = "floorp.desktop";
      "x-scheme-handler/notion" = "notion-app-enhanced.desktop";
      "x-scheme-handler/obsidian" = "obsidian.desktop";
      "x-scheme-handler/sgnl" = "signal.desktop";
      "x-scheme-handler/signalcaptcha" = "signal.desktop";
      "x-scheme-handler/slack" = "slack.desktop";
      "x-scheme-handler/tel" = "org.kde.kdeconnect.handler.desktop";
      "x-scheme-handler/sidequest" = "SideQuest.desktop";
      "x-scheme-handler/logseq" = "Logseq.desktop";
      "x-scheme-handler/discord" = "vesktop.desktop";
      "x-scheme-handler/tg" = "org.telegram.desktop.desktop";
      "x-scheme-handler/tonsite" = "org.telegram.desktop.desktop";
      "x-scheme-handler/ror2mm" = "r2modman.desktop";
      "x-scheme-handler/discord-1216669957799018608" = "discord-1216669957799018608.desktop";
      "video/vnd.mpegurl" = "mpv.desktop";
      "video/x-flic" = "mpv.desktop";
      "video/3gpp2" = "mpv.desktop";
      "video/vnd.avi" = "mpv.desktop";
      "video/mp2t" = "mpv.desktop";
      "video/x-flv" = "mpv.desktop";
      "video/vnd.rn-realvideo" = "mpv.desktop";
      "video/quicktime" = "mpv.desktop";
      "video/mpeg" = "mpv.desktop";
      "video/mp4" = "mpv.desktop";
      "video/dv" = "mpv.desktop";
      "video/webm" = "mpv.desktop";
      "text/plain" = "Helix.desktop";
      "text/x-pascal" = "Helix.desktop";
      "text/x-java" = "Helix.desktop";
      "text/x-chdr" = "Helix.desktop";
      "text/spreadsheet" = "Helix.desktop";
      "text/x-c++hdr" = "Helix.desktop";
      "text/x-tex" = "Helix.desktop";
      "text/csv" = "Helix.desktop";
      "text/x-csrc" = "Helix.desktop";
      "text/tab-separated-values" = "Helix.desktop";
      "text/tcl" = "Helix.desktop";
      "text/x-moc" = "Helix.desktop";
      "text/x-makefile" = "Helix.desktop";
      "text/x-c++src" = "Helix.desktop";
    };
  };
  xdg.configFile."mimeapps.list".force = true;
  xdg.dataFile."applications/mimeapps.list".force = true;

  # Compose key. niri maps Caps -> Multi_key (xkb `compose:caps`); this builds
  # the ~/.XCompose that every client (foot, fcitx5, GTK, Qt) loads via
  # libxkbcommon. Order matters — later entries win exact-duplicate sequences:
  #   1. the FULL default table (é ñ ç → © ½ « » …). NixOS ships no
  #      /usr/share/X11/locale, so the usual `include "%L"` can't resolve;
  #      point straight at libX11's copy in the store (Nix keeps it current).
  #   2. kragen/xcompose (vendored) — comprehensive math, arrows, sub/super-
  #      scripts, double-struck, IPA, typography.
  #   3. our local block last — Greek (Caps g a -> α) + Slack-style :emoji:
  #      (Caps : f i r e : -> 🔥) — so it overrides on any clash.
  home.file.".XCompose".text =
    ''
      include "${pkgs.xorg.libX11}/share/X11/locale/en_US.UTF-8/Compose"
    ''
    + builtins.readFile ./_files/XCompose.kragen
    + "\n"
    + builtins.readFile ./_files/XCompose;

  home.pointerCursor = {
    name = "Adwaita";
    package = pkgs.adwaita-icon-theme;
    size = 24;
    x11 = {
      enable = true;
      defaultCursor = "Adwaita";
    };
  };
}
