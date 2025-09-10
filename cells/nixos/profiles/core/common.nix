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
      libappindicator-gtk3
      vulkan-loader
      xorg.libX11
      xorg.libXScrnSaver
      xorg.libXcomposite
      xorg.libXcursor
      xorg.libXdamage
      xorg.libXext
      xorg.libXfixes
      xorg.libXi
      xorg.libXrandr
      xorg.libXrender
      xorg.libXtst
      xorg.libxcb
      xorg.libxkbfile
      xorg.libxshmfence
    ];
in {
  imports = [
    # Include minimal configuration
    # inputs.nixos.nixosProfiles.core.minimal
    #builtins.trace [inputs common] ./minimal
  ];

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
        noto-fonts-emoji
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
    fcitx5.addons = with pkgs; [ fcitx5-mozc fcitx5-gtk fcitx5-configtool ];
    fcitx5.waylandFrontend = true;
  };
  # Set necessary environment variables for Wayland
  environment.sessionVariables = {
    XMODIFIERS = "@im=fcitx";
    QT_IM_MODULE = "fcitx";
  };

  # automatic timezone setting
  # services.automatic-timezoned.enable = true; #idk why this doesn't work :/
  time.timeZone = "America/New_York";
  # Using NetworkManager because it is easy
  networking.networkmanager.enable = lib.mkDefault true;
}
