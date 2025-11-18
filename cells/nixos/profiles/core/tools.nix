{ inputs, common, }:
{ lib, config, pkgs, ... }:
with lib; {
  environment.systemPackages = with pkgs; [
    # File Tools
    unzip # Unzip
    zip # Zip
    unar # general uncompression tool (with subfolder autodetection)
    file # Print File Info
    curl # Clone URL
    # micro # Micro Text Editor
    neovim # Best CLI Text Editor
    killall # Kill programs by name
    tree # Tree list directory
    fd # Alternative to `find`
    ripgrep # Alternative to `grep`
    zellij # Terminal Multiplexer
    yazi # Terminal gui preview
    gnome-epub-thumbnailer # epub preview for yazi

    # Analysis Tools
    inxi # System info
    fastfetch # for r/unixporn
    dua # Disk Usage Analyzer
    nmap # Network Mapper
    traceroute # traceroute
    lsof # List open files
    usbutils # USB utilities
    pciutils # PCI utilities
    htop # Print process info
    speedtest-cli # Test network speed
    pavucontrol # Control Sound from CLI
    tokei # Code counter
    lm_sensors # Sensors
    acpi # Power Tools
    powertop # Top Power Consuming Applications
    powerstat # tool to monitor power usage
    whois # DNS lookup
    dmidecode # Hardware/Firmware info
    drm_info # Display renderer manager info
    libva-utils # video acceleration info
    sbctl

    # Nix Tools
    nix-tree # Nix dependency tree viewer
    nixpkgs-review # Review nixpkgs pull requests
    nix-index # Generte Index of /nix/store
    direnv # Auto shell when cd into directory

    binutils # Manipulating Binaries
    coreutils # GNU Coreutils
    moreutils # More Utilities
    dnsutils # DNS Utilities
    iputils # IP utilities
    inotify-tools # File Watching tools
    jq # Json processor
    usbutils # Usb utilities
    util-linux # Linux utilities
    smartmontools # Drive Health tools
    ffmpeg # Video Converter
    imagemagick # Image Converter
    sshfs # Mount remote filesystems
    mpv # video playing
    hyperfine # Benchmarking tool

    # Extra tools
    yt-dlp

    # appimage
    (appimage-run.override { extraPkgs = pkgs: [ libthai ]; })
  ];
  # Set appimage-run as default interpreter for AppImage executables
  boot.binfmt.registrations.appimage = {
    wrapInterpreterInShell = false;
    interpreter = "${pkgs.appimage-run}/bin/appimage-run";
    recognitionType = "magic";
    offset = 0;
    mask = "\\xff\\xff\\xff\\xff\\x00\\x00\\x00\\x00\\xff\\xff\\xff";
    magicOrExtension = "\\x7fELF....AI\\x02";
  };
}
