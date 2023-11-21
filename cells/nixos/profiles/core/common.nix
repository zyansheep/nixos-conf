{
  inputs,
  common,
}: {lib, pkgs, ...}: {
  imports =
    [
      # Include minimal configuration
      # ./minimal.nix
    ];

  environment.systemPackages = with pkgs; [
    firefox-wayland # Browser
    neovim

    # Pipewire
    plasma-pa
  ];

  # Fan Control & Power Management
  services.tlp.enable = true;
  services.power-profiles-daemon.enable = false;

  # MDNS
  services.avahi.enable = true;

  # Touchpad
  services.xserver.libinput = {
    enable = true;
    touchpad.naturalScrolling = true;
  };

  # Automatically clear old packages
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };

  # Fonts
  # Font Declaration
  fonts = {
    fonts = with pkgs; [
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
    alsa.support32Bit = true;
    pulse.enable = true;
    jack.enable = true;
    # media-session.enable = true;
  };

  # Using NetworkManager because it is easy
  networking.networkmanager.enable = true;

  # Fix device mounting as cdrom instead of normally (because it has built-in drivers)
  # hardware.usb-modeswitch.enable = true;

  # Set backend to iwd instead of wpa_supplicant to fix slow reconnect on unsuspend bug
  networking.networkmanager.wifi.backend = "wpa_supplicant";
}
