# Japanese Weave (github:zyansheep/japanese-weave): the Claude Code plugin and the browser extension's native host,
# from the flake input, so a merge to its main reaches the machines like any other change (input-bump on functor moves
# the input; nixos-ci and nixos-deploy, functor first, do the rest). Claude Code's marketplace path is
# ~/.local/share/japanese-weave/current (~/.claude/settings.json, extraKnownMarketplaces.japanese-weave). The word
# state stays in ~/.japanese-weave; functor runs the collector (hosts/functor). The Floorp extension is still
# installed by hand: `fw extension`, then "Add".
{inputs, ...}: {lib, ...}: {
  imports = [inputs.japanese-weave.homeManagerModules.default];

  services.japanese-weave.client.enable = true;

  # `fw setup` wrote these by hand before; home-manager takes them over.
  home.file = lib.genAttrs [
    ".mozilla/native-messaging-hosts/com.japaneseweave.host.json"
    ".config/google-chrome/NativeMessagingHosts/com.japaneseweave.host.json"
    ".config/chromium/NativeMessagingHosts/com.japaneseweave.host.json"
  ] (_: {force = true;});
}
