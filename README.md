# Nix Configuration

Zyan's Nix config (adapted from truelector, thank you!)


Rebuild: `nrb`
Rebuild & update flake `nrbu`

## isomorph networking

NetworkManager owns Wi-Fi/wired connections, DHCP and Proton's VPN interfaces.
iwd is its Wi-Fi authentication backend; iwd's built-in network configuration
and standalone dhcpcd are disabled. systemd-resolved handles DNS updates from
NetworkManager and Tailscale. The Tailscale interface and Hampshire's PPP
interfaces remain under their respective services.

- Click Waybar's Wi-Fi text or press Alt+Shift+W to toggle the interactive
  [Network Manager Sidebar](https://github.com/Relz/network-manager-sidebar).
  It scans, connects to networks, prompts for Wi-Fi passwords, and toggles Wi-Fi.
  Clicking the connected network opens its details; its separate disconnect
  button disconnects without forgetting the password. The details page has
  Disconnect/Connect, Forget (with confirmation), and Edit controls.
  Edit and Add Wi-Fi open native pages inside the sidebar. They support profile
  and network names, passwords, autoconnect, metered status, hidden networks,
  IPv4/IPv6 methods, addresses, gateways and DNS. New enterprise profiles support
  PEAP/MSCHAPv2 and TTLS/PAP with server-domain and CA-certificate validation;
  existing enterprise EAP/certificate settings are retained while identity,
  password and server-domain fields can be edited. Other unedited settings are
  preserved. Save writes to persistent storage; reconnect to apply the changes.
  Waybar still shows signal, frequency, address, gateway and traffic rates on
  hover. There is no separate network tray applet.
- Open Proton VPN from the application launcher or the Services popup. Use its
  GUI for connecting, server selection, settings and the kill switch. Do not
  activate its generated NetworkManager profiles by hand. There is no custom
  CLI controller or WireGuard configuration scraper.
- Saved credentials stay outside this repository. `/var/lib/iwd`,
  `/var/lib/NetworkManager` and `/etc/NetworkManager/system-connections` persist
  across reboots. NetworkManager's iwd backend creates mirror connections for
  existing iwd KnownNetworks, including externally configured enterprise Wi-Fi.
  Edit connections through NetworkManager after the migration.

The sidebar is pinned in `cells/common/packages/nm-sidebar.nix`; it uses libnm
directly, with no connection-parsing scripts or separate credential store.
Its `nm-sidebar` user service starts hidden with the graphical session and is
replaced on upgrades. The Waybar service tracks configuration changes so a
rebuild reloads changed click commands. The IPC socket is in `$XDG_RUNTIME_DIR`.
The local extension and regression tests are in `cells/common/patches/nm-sidebar`.
Edits use a private draft, preserve saved secrets, and reject conflicting changes.
The external editor remains a private dependency for upstream VPN-profile
editing only; Wi-Fi editing is native and nm-applet does not autostart.
Proton's generated profiles may appear in its VPN list; continue using Proton's
own app to control those connections and the kill switch.

### First activation from the standalone-iwd build

Build before switching, and keep the working generation available in the boot
menu. Back up iwd's profiles outside the Nix store because NetworkManager can
update them when connections are edited. Run these commands in a local terminal:

```sh
sudo cp -a /var/lib/iwd "/persist/iwd-before-networkmanager-$(date +%Y%m%d-%H%M%S)"
sudo nixos-rebuild switch --flake .#isomorph
sudo systemctl restart tailscaled
```

The switch may briefly disconnect Wi-Fi. Restarting Tailscale makes it detect
the new resolved DNS backend. Log out and back in to pick up group membership
and the new session packages. The old iwmenu/Impala tools bypass NetworkManager;
use the sidebar instead. If updating from the nm-applet generation, logging out
also stops the previously autostarted tray icon.

Verify before rebooting:

```sh
nmcli device status
nmcli connection show --active
systemctl is-active NetworkManager iwd systemd-resolved
systemctl is-active dhcpcd # should be inactive / not found
resolvectl status
getent hosts example.com
```

Then test a tailnet hostname, Wi-Fi reconnect, and Proton connect/disconnect
using its GUI. Check the public address and `resolvectl status` with Proton
connected. Test a cold boot only after these checks pass.

Tailscale currently advertises global DNS as well as MagicDNS. Enabling resolved
does not change that tailnet policy or guarantee Proton-only DNS when both VPNs
are running. Inspect the per-link DNS domains (especially `~.`), and test
Tailscale/Hampshire access with Proton's kill switch before relying on concurrent
VPN operation. DNS policy changes belong in the tailnet configuration rather
than a local script that repeatedly rewrites resolv.conf.

If connection activation fails, use the sidebar to select the saved network.
For recovery, boot the previous working generation. The original iwd profiles
are retained, with the timestamped backup available if any were edited.
