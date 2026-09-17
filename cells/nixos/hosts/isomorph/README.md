# isomorph

This host uses the Niri desktop. Host settings and persistence declarations are
in [default.nix](default.nix); desktop services are in the
[Niri profile](../../profiles/graphics/niri.nix). For bar controls and appearance,
see the [Waybar guide](../../../../dotfiles/.config/waybar/README.md).

## Connecting and configuring networks

Click Waybar's Wi-Fi name or icon, or press `Alt+Shift+W`, to open the interactive
network sidebar. It scans, connects, prompts for passwords and toggles Wi-Fi.
Open a connected network for details and editable settings. Disconnect keeps the
saved profile; Forget has a confirmation. Save persists edits; reconnect to apply
them. Back discards an unsaved draft, and edited fields have undo controls.

Use the official Proton VPN app from the launcher or Services popup for login,
connections, server selection, settings and the kill switch. Do not activate its
generated NetworkManager profiles separately, including entries that appear in
the sidebar's VPN list. The [Proton profile](../../profiles/services/protonvpn.nix)
installs the app; there is no custom VPN controller or WireGuard config scraper.

## Network ownership

| Component | Responsibility |
| --- | --- |
| NetworkManager | Wi-Fi/wired connections, DHCP, and Proton's VPN interfaces |
| iwd | Wi-Fi authentication, with its built-in network configuration disabled |
| systemd-resolved | DNS updates from NetworkManager and Tailscale |
| Tailscale / Hampshire VPN services | Their own `tailscale0` / `ppp*` interfaces, excluded from NetworkManager |

Standalone dhcpcd is disabled so it does not compete for addresses and routes.
There is no separate network tray applet. The relevant VPN services are
[tailscale.nix](../../profiles/services/tailscale.nix) and
[hampshire-vpn.nix](../../profiles/services/hampshire-vpn.nix).

The sidebar is pinned in [nm-sidebar.nix](../../../common/packages/nm-sidebar.nix)
and uses libnm directly. Its native settings extension, save/conflict handling
and regression tests are documented [beside the patch](../../../common/patches/nm-sidebar/README.md).

## Saved connections and MAC addresses

Credentials stay outside this repository. These directories persist across root
rollback and reboot through the host's `/persist` configuration:

- `/var/lib/iwd`
- `/var/lib/NetworkManager`
- `/etc/NetworkManager/system-connections`

NetworkManager's iwd backend mirrors existing iwd KnownNetworks, including
externally configured enterprise Wi-Fi. Edit connections through NetworkManager;
the sidebar does not maintain its own credential store.

The default Wi-Fi MAC is stable per network: NetworkManager uses `stable-ssid`
and iwd uses `General.AddressRandomization=network`. NetworkManager's setting
alone is insufficient with this backend. The sidebar shows the policy name
directly, and explicit per-profile overrides are retained. Its Device address
choice saves the adapter's literal MAC because iwd ignores the `permanent`
keyword. See the extension README for supported policies and draft semantics.

## DNS with concurrent VPNs

Enabling resolved does not change the tailnet's DNS policy. If Tailscale
advertises global DNS alongside MagicDNS, do not assume Proton-only DNS while
both VPNs are running. Inspect the per-link DNS domains, especially `~.`, and
test Tailscale/Hampshire access with Proton's kill switch before relying on
concurrent VPN operation. Tailnet DNS policy belongs in the tailnet configuration,
not a local script that repeatedly rewrites `resolv.conf`.

## Application launcher

`Super+D` toggles the resident Vicinae launcher; `Super+Shift+D` opens Rofi as a
fallback. Native/Flatpak subtitles come from desktop metadata. Window matching
uses process identity so two installations with the same window class remain
separate. The package patch and regression tests are in
[the Vicinae patch directory](../../../common/patches/vicinae).

User preferences live in `~/.config/vicinae/settings.json`. Automatic file
indexing, file results in root search and the input-injection server were disabled
for the minimal trial; these preferences are not enforced by Nix. No browser
integration is configured.

## Flatpak font troubleshooting

If Flatseal shows missing glyphs, rebuild its font cache and restart it:

```sh
flatpak run --command=fc-cache com.github.tchx84.Flatseal -f
```

This repairs the cache without resetting permissions or app data. Nix's fixed
font-directory timestamps can leave stale caches after updates. In an isolated
Flatseal-runtime test, [nixpkgs PR #529277](https://github.com/NixOS/nixpkgs/pull/529277)'s
path change bypassed a stale cache once, but another font update reproduced the
failure. A generation-specific cache identity passed that test; no such Flatpak
package patch is deployed here. The original glyph failure's cause was not
conclusively established before its cache was repaired.
