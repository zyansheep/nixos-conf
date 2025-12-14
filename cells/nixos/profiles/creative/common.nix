{ inputs, common, }:
{ pkgs, ... }: {
  environment.systemPackages = with pkgs; [
    blender # It's Blender!
    #krita 			# Image Editor
    inkscape # Vector Art Editor
    godot_4 # Game Engine
    audacity # Audio editor

    ffmpeg-full # Video Converter
    yt-dlp # Video Downloader
    kdePackages.kdenlive # Video Editor
    obs-studio # Screen Recorder
  ];
  hardware.opentabletdriver.enable = true;
}
