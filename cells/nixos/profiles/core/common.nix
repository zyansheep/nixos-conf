{
  inputs,
  common,
}: {
  lib,
  pkgs,
  ...
}: {
  imports = [
    # Include minimal configuration
    # inputs.nixos.nixosProfiles.core.minimal
    #builtins.trace [inputs common] ./minimal
  ];

  # default packages
  environment.systemPackages = with pkgs; [
    firefox # Browser
  ];

  # MDNS
  # services.avahi.enable = true;
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
  };

  # Touchpad
  # services.xserver.libinput = {
  #   enable = true;
  #   touchpad.naturalScrolling = true;
  # };

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
      monospace = ["DejaVu Sans Mono for Powerline"];
      sansSerif = ["DejaVu Sans"];
    };
  };

  # Use Pipewire for sound
  # security.rtkit.enable = true;
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    # alsa.support32Bit = true;
    pulse.enable = true;
    # jack.enable = true;
    # media-session.enable = true;
  };
  # hardware.pulseaudio.enable = true;

  # Using NetworkManager because it is easy
  networking.networkmanager.enable = true;
}
