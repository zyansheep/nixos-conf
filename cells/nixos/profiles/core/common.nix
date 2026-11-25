{ inputs, common, }:
{ config, pkgs, lib, ... }:

let
  commonPackages = with pkgs;
    [
      curl
      libxml2
      xz
      systemd
      acl
      attr
      bzip2
      dbus
      expat
      fontconfig
      freetype
      fuse3
      icu
      libnotify
      libsodium
      libssh
      libunwind
      libusb1
      libuuid
      nspr
      nss
      stdenv.cc.cc
      util-linux
      zlib
      zstd
      SDL
      SDL2
      openssl
      libgcc
      ffmpeg-full
    ] ++ lib.optionals (config.hardware.graphics.enable) [
      wayland
      pipewire
      cups
      libxkbcommon
      pango
      mesa
      libdrm
      libglvnd
      libpulseaudio
      atk
      cairo
      alsa-lib
      at-spi2-atk
      at-spi2-core
      gdk-pixbuf
      glib
      gtk3
      libGL
      libgbm
      libappindicator-gtk3
      vulkan-loader
      libx11
      libxscrnsaver
      libxcomposite
      libxcursor
      libxdamage
      libxext
      libxfixes
      libxi
      libxrandr
      libxrender
      libxtst
      libxcb
      libxkbfile
      libxshmfence
    ];
in {
  # default packages
  environment.systemPackages = with pkgs; [ pkg-config ] ++ commonPackages;
  programs.firefox.enable = lib.mkDefault true;

  # MDNS
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Fonts
  # Font Declaration
  fonts = {
    packages = with pkgs;
      [
        font-awesome
        dejavu_fonts
        source-code-pro
        noto-fonts
        noto-fonts-color-emoji
        powerline-fonts

        # nihongo fonts
        noto-fonts-cjk-sans
        ipafont
      ] ++ (builtins.filter lib.attrsets.isDerivation
        (builtins.attrValues pkgs.nerd-fonts));
    fontconfig.defaultFonts = {
      monospace = [ "DejaVu Sans Mono for Powerline" "IPAGothic" ];
      sansSerif = [ "DejaVu Sans" "IPAPGothic" ];
      serif = [ "DejaVu Serif" "IPAPMincho" ];
    };
  };

  # Use Pipewire for sound
  security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    # alsa.support32Bit = true;
    pulse.enable = true;
    # jack.enable = true;
    # media-session.enable = true;
  };

  # run non nix-linked programs
  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = commonPackages;

  i18n.inputMethod = {
    type = "fcitx5";
    enable = true;
    fcitx5.addons = with pkgs; [
      fcitx5-mozc
      fcitx5-gtk
      qt6Packages.fcitx5-configtool
    ];
    fcitx5.waylandFrontend = true;
  };

  # automatic timezone setting
  # services.automatic-timezoned.enable = true; #idk why this doesn't work :/
  time.timeZone = "America/Los_Angeles";
  # Using NetworkManager because it is easy
  networking.networkmanager.enable = lib.mkDefault true;

  # /tmp: delete what nobody created or changed in 3 days (the daily systemd-tmpfiles-clean run). Reading a file does
  # not keep it: systemd's own tmp.conf ages /tmp by every timestamp, access time included, at 10 days, so a grep, a
  # backup or an agent reading through /tmp kept old files alive for good (41G on functor, 2026-11-25). bcm/BM = birth,
  # status change and modification time for files, birth and modification time for directories. 00-nixos.conf sorts
  # before tmp.conf, so this line wins for /tmp. Listening sockets and flock()ed paths are kept. Agents are told by
  # their global instructions.
  systemd.tmpfiles.rules = [ "q /tmp 1777 root root bcmBM:3d" ];
}
