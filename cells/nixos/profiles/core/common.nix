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
  # environment.systemPackages = with pkgs; [];
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
      monospace = ["DejaVu Sans Mono for Powerline"];
      sansSerif = ["DejaVu Sans"];
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
  programs.nix-ld.libraries = with pkgs; [
    xorg.libX11
    xorg.libXcursor
    xorg.libxcb
    xorg.libXi
    libxkbcommon
    wayland
  ];

  # automatic timezone setting
  services.automatic-timezoned.enable = true;
  time.timeZone = "America/New_York";
  # Using NetworkManager because it is easy
  networking.networkmanager.enable = true;
}
