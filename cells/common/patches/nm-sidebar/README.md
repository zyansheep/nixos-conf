# Native Wi-Fi settings extension

Applied to the pinned upstream source in `../../packages/nm-sidebar.nix`.
The zero-context patch changes the upstream call sites; the new C files are
copied into the source tree during `postPatch`.

- `connection-settings.c`: details, confirmation and editor navigation pages.
- `profile-model.c`: validates IP fields on an editor-owned clone. Untouched
  addresses, routes and other settings keep their existing attributes.
- `profile-save.c`: retrieves and merges saved secrets, persists with Update2,
  and checks for concurrent changes. iwd can change a profile's version during
  GetSecrets, so the post-read check also compares the non-secret settings.
- `integrated-settings.patch`: connects the pages to the Wi-Fi rows and the
  sidebar's navigation view. Also fixes an upstream scroll-adjustment notify
  callback that used the wrong signature and could crash on allocation.

Wi-Fi edit/add pages are native. The upstream external editor remains available
for VPN profiles. Existing EAP methods/certificates, MAC policies, custom routes,
and settings without a field in the form are preserved. Save does not reconnect
an active network. Back discards the draft. A credentials-read failure prevents
saving rather than risking removal of a saved secret.

`nix build .#nm-sidebar` runs the isolated model regression tests. For the live
libnm persistence check, run `bash cells/common/patches/nm-sidebar/test-live-save.sh`
from an active desktop session with permission to manage NetworkManager profiles.
It creates temporary **disconnected, autoconnect-disabled** profiles with dummy
credentials, tests WPA and enterprise password preservation/replacement and
conflict rejection, and deletes each profile afterward. It never activates them.
If the test process is forcibly terminated, remove any `nm-sidebar-test-*`
profile it left behind using the sidebar's saved-network details page.

UI smoke check: toggle from Waybar, open a connected row, check details, open
Edit, change a field and go Back without saving, and open/cancel Forget. Verify
Wi-Fi stays connected. A full NixOS build checks the sidebar user service and
Waybar configuration restart trigger together.
