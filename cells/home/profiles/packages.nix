_: {pkgs, ...}: {
  home.packages = with pkgs; [
    # Browsers
    brave
    chromium
    floorp-bin
    librewolf
    tor-browser

    # Communication
    element-desktop
    slack
    telegram-desktop
    vesktop

    # Media
    audacity
    blender
    digikam
    inkscape
    krita
    lmms
    musescore
    nomacs
    obs-studio
    pinta
    vital

    # Office / Documents
    calibre
    libreoffice
    libreoffice-qt
    notesnook
    obsidian
    zotero

    # PDF / Text
    pandoc
    pdftk
    poppler-utils
    python313Packages.pdftotext
    tectonic
    tesseract
    texlab
    texliveFull
    texstudio
    tinymist
    typst
    typstyle

    # Dev tools
    bun
    claude-code
    code-cursor
    devenv
    duckdb
    gh
    glfw
    google-cloud-sdk
    micromamba
    nasm
    nixfmt-classic
    nix-search-cli
    ollama
    pijul
    qwen-code
    ripgrep-all
    sccache
    serpl
    sqlite
    svelte-language-server
    tmuxai
    uv

    # Gaming
    gamescope
    protontricks
    r2modman
    wine

    # System / Utilities
    activitywatch
    afetch
    bashmount
    btop
    colorized-logs
    cpulimit
    flyctl
    fontpreview
    hello
    iw
    jre
    localsend
    logmein-hamachi
    opendrop
    openfortivpn
    osmctools
    parted
    pbzip2
    qbittorrent
    swappy
    syncthing
    sysbench
    termscp
    trash-cli
    udiskie
    wev
    wf-recorder
    wlsunset
    wtype

    # Wayland / Desktop
    bemoji
    rofimoji
    tofi
    walker

    # 3D Printing / CAD
    cura-appimage
    ipe

    # Video
    davinci-resolve
    losslesscut-bin

    # QGIS
    qgis

    # Sideloading
    sidequest
  ];
}
