# Native Wi-Fi settings extension

Applied to the pinned [upstream sidebar](https://github.com/Relz/network-manager-sidebar)
in [nm-sidebar.nix](../../packages/nm-sidebar.nix). For connection usage,
storage and migration, see [isomorph networking](../../../nixos/hosts/isomorph/README.md).

## Implementation

The zero-context patch changes the upstream call sites; the new C files are
copied into the source tree during `postPatch`.

- `connection-settings.c`: combined details/settings page and Forget confirmation.
  A small field registry tracks original values, dirty styling and per-field
  undo. Live labels update independently of draft controls. Saving rebases the
  form without navigating away; libnm versions are adopted only when the cached
  settings match the saved snapshot. Drafts are normalized before saving so
  derived properties (such as legacy MAC randomization) match NetworkManager.
- `settings.css`: compact form styling scoped to these pages.
  The Connection Information overview and per-profile diagnostics share the
  same selectable label/value rows, with 12px text and minimal vertical padding.
- `profile-model.c`: validates IP fields on an editor-owned clone. Untouched
  addresses, routes and other settings keep their existing attributes.
- `profile-save.c`: retrieves and merges saved secrets, persists with Update2,
  and checks for concurrent changes. iwd can change a profile's version during
  GetSecrets, so the post-read check also compares the non-secret settings.
- `integrated-settings.patch`: connects the pages to the Wi-Fi rows and the
  sidebar's navigation view. Also fixes an upstream scroll-adjustment notify
  callback that used the wrong signature and could crash on allocation.

Wi-Fi settings/add pages are native. The upstream external editor remains available
for VPN profiles. Existing EAP methods/certificates, custom routes,
and settings without a field in the form are preserved. Save does not reconnect
an active network. Back discards the draft. A credentials-read failure prevents
saving rather than risking removal of a saved secret.

## Layout and editability

- Connect/Disconnect and Auto-connect share one row. Only explicit Save persists
  a switch, text or dropdown edit. The fixed bottom bar appears when dirty, with
  a changed-field count, Revert all and Save changes. Each edited field also has
  its own undo button; reverting to the original value clears the dirty state.
- Names, replacement password, hidden SSID, metering and common MAC policies are
  compact editable rows. A blank password retains the saved secret. Existing
  custom MAC addresses remain available as an option without being overwritten.
  The MAC dropdown shows policy names without a separate Default entry. Its
  inherited value is compiled from `networking.networkmanager.wifi.macAddress`
  by the Niri profile (currently `stable-ssid`, shown as Stable per network).
  Saving other fields leaves an inherited MAC policy unset; changing the policy
  stores an explicit per-network override.
  With iwd, `General.AddressRandomization=network` implements the default and
  enables NM's mirrored `AlwaysRandomizeAddress`/`AddressOverride` options.
  Unsupported Keep current/Stable per profile choices are omitted, and Device
  address writes the compatible adapter's permanent MAC as a literal override.
  Legacy unsupported keywords resolve to the global policy actually used by
  iwd. The wpa_supplicant backend retains the full NetworkManager policy list.
- IPv4/IPv6 expanders summarize saved method and DNS policy. Addresses and DNS
  entered there are saved overrides, not the currently assigned values.
- Live IPs, gateways and DNS are selectable text below the form. Device state,
  hardware address, driver, BSSID, radio, routes and UUID belong in diagnostics.
  Existing authentication methods, certificates and BSSID/band locks are displayed
  there and retained; the form does not implement every NetworkManager option.

New enterprise connections support PEAP/MSCHAPv2 and TTLS/PAP with server-domain
and CA-certificate validation. Existing enterprise methods and certificates are
preserved while identity, password and server-domain fields can be edited.

## Runtime integration

The [Niri profile](../../../nixos/profiles/graphics/niri.nix) starts the
`nm-sidebar` user service hidden with the graphical session and replaces it on
upgrades. The CLI sends requests through an IPC socket in `$XDG_RUNTIME_DIR`.
Waybar and `Alt+Shift+W` toggle this same process. The sidebar uses libnm directly,
with no connection-parsing scripts or separate credential store.

The upstream external editor is a private runtime dependency for VPN profiles;
native Wi-Fi editing does not launch it, and nm-applet does not autostart.
Control Proton's generated profiles and kill switch through Proton's own app.

## Verification

Run these commands from the repository root.

`nix build .#nm-sidebar` runs the isolated model regression tests. For the live
libnm persistence check, run `bash cells/common/patches/nm-sidebar/test-live-save.sh`
from an active desktop session with permission to manage NetworkManager profiles.
It creates temporary **disconnected, autoconnect-disabled** profiles with dummy
credentials, tests WPA and enterprise password preservation/replacement and
conflict rejection, and deletes each profile afterward. It never activates them.
If the test process is forcibly terminated, remove any `nm-sidebar-test-*`
profile it left behind using the sidebar's saved-network details page.

Add `--ui` to the live test command to exercise the actual form on a temporary
disconnected profile (requires a graphical session; no test window is shown).
It checks draft isolation, per-field undo, Revert all, conditional Save visibility,
validation, repeated saves, password-input reset, disabled-IP normalization and
MAC policy display/preservation for both custom addresses and inherited defaults.

UI smoke check: toggle from Waybar, open a connected row, edit/revert a field,
expand IP settings and diagnostics, and open/cancel Forget. Verify Wi-Fi stays
connected. A full NixOS build checks the sidebar user service and Waybar
configuration restart trigger together.
