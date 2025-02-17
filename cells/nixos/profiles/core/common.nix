{ inputs, common, }:
{ lib, pkgs, config, ... }: {
  imports = [
    # Include minimal configuration
    # inputs.nixos.nixosProfiles.core.minimal
    #builtins.trace [inputs common] ./minimal
  ];

  # default packages
  environment.systemPackages = with pkgs; [ pkg-config ];
  programs.firefox.enable = true;

  # MDNS
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Fonts
  # Font Declaration
  fonts = {
    packages = with pkgs; [
      powerline-fonts
      dejavu_fonts
      font-awesome
      noto-fonts
      noto-fonts-emoji
      source-code-pro
    ];
    fontconfig.defaultFonts = {
      monospace = [ "DejaVu Sans Mono for Powerline" ];
      sansSerif = [ "DejaVu Sans" ];
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

  programs.nix-ld.libraries = with pkgs;
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

  # automatic timezone setting
  # services.automatic-timezoned.enable = true; #idk why this doesn't work :/
  time.timeZone = "America/New_York";
  # Using NetworkManager because it is easy
  networking.networkmanager.enable = lib.mkDefault true;
}
