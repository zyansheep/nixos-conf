{
  inputs,
  common,
}: { lib, config, pkgs, ... }:
with lib;
{
  environment.systemPackages = with pkgs; [
    # File Tools
    unzip # Unzip
    file # Print File Info
    curl # Clone URL
    # micro # Micro Text Editor
    neovim # Best CLI Text Editor
    killall # Kill programs by name
    tree # Tree list directory

    # Analysis Tools
    dua # Disk Usage Analyzer
    nmap # Network Mapper
    lsof # List open files
    usbutils # USB utilities
    pciutils # PCI utilities
    neofetch # Print System Details
    htop # Print process info
    speedtest-cli # Test network speed
    pavucontrol # Control Sound from CLI
    tokei # Code counter
    fd # Alternative to `find`
    ripgrep # Alternative to `grep`
    hyperfine # Benchmarking tool
    zellij # Terminal Multiplexer
    lm_sensors # Sensors
    acpi # Power Tools
    powertop # Top Power Consuming Applications

    # Nix Tools
    nix-tree # Nix dependency tree viewer
    nixpkgs-review # Review nixpkgs pull requests
    nix-index # Generte Index of /nix/store
    manix # Doc searcher for nix
    direnv # Auto shell when cd into directory

    binutils # Manipulating Binaries
    coreutils # GNU Coreutils
    moreutils # More Utilities
    dnsutils # DNS Utilities
    iputils # IP utilities
    jq # Json processor
    usbutils # Usb utilities
    util-linux # Linux utilities
    whois # DNS lookup
    smartmontools # Drive Health tools
    ffmpeg # Video Converter
    imagemagick # Image Converter
    sshfs # Mount remote filesystems
    atool # Archive Tools
    mpv

    # Extra tools 
    glxinfo
    libva-utils
    inxi
    
    # appimage
    (appimage-run.override { extraPkgs = (pkgs: [ libthai ]); })
  ];
  # Set appimage-run as default interpreter for AppImage executables
  boot.binfmt.registrations.appimage = {
    wrapInterpreterInShell = false;
    interpreter = "${pkgs.appimage-run}/bin/appimage-run";
    recognitionType = "magic";
    offset = 0;
    mask = ''\xff\xff\xff\xff\x00\x00\x00\x00\xff\xff\xff'';
    magicOrExtension = ''\x7fELF....AI\x02'';
  };
}
