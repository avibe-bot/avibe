

## Desktop TEST installation

This GitHub prerelease is for desktop testing. Download the installer matching
your machine: `aarch64-apple-darwin.dmg` for Apple silicon,
`x86_64-apple-darwin.dmg` for Intel Macs, or `x86_64-pc-windows-msvc.exe` for
Windows x64. Each filename starts with `Avibe_<version>_`.

Download the matching `.SHA256SUMS`, `.SIGNATURE`, `.SOURCE.json`, and
`.runtime-manifest.json` files too. On macOS, run `shasum -a 256 -c` with the
downloaded `.SHA256SUMS` filename in the directory containing all five files.
On Windows, use `Get-FileHash -Algorithm SHA256 <installer.exe>` and compare it
with the installer entry in `.SHA256SUMS`. The source record identifies the tag,
commit, desktop version, and bundled Avibe package version; the Runtime manifest
records the embedded archive and toolchain hashes. On macOS, the source record
also identifies the pinned Cua Driver source and its archive, extracted,
thinned, packaged, and code-signing hashes. `SIGNATURE` is descriptive
metadata naming the signed layers, **not a cryptographic signature file**.
Hashes detect changed downloads; on macOS, Apple's signature and notarization
establish the developer identity.

On macOS, the DMG, the app inside it, and every program bundled with the app are
signed with Avibe's Developer ID (Team `68B2SQ6YK3`) and notarized by Apple,
with the tickets stapled. Open the DMG, drag Avibe to Applications, and launch
it; macOS opens it after its usual first-launch confirmation, with no Privacy &
Security override. If macOS instead says Avibe is damaged or cannot be verified, do not
override it: delete the download and fetch it again. To check a download
yourself, `spctl --assess --type open --context context:primary-signature -v
<file>.dmg` and, after installing, `spctl --assess -v /Applications/Avibe.app`
both report `source=Notarized Developer ID`.

Every signed release keeps the same code identity, so macOS keeps Computer Use's
Accessibility and Screen Recording permissions, and Keychain access you allowed,
across updates. Earlier ad-hoc builds changed identity with every build: after
moving from one of them to a signed build, grant those permissions once more.

The Windows NSIS installer is unsigned and may show SmartScreen or an unknown
publisher prompt. After checking the download, use More info → Run anyway only
if your machine's policy permits it. Keep Gatekeeper, SmartScreen, and antivirus
enabled; managed machines may require administrator approval.

Python, Avibe, Node, and npm are bundled; no separate installation is required.
Agent backends are detected or installed when selected in Avibe.

To upgrade, use the desktop updater when your installed release published signed
update metadata (`desktop-update-<target>.json` assets; the first was
`gh-v3.1.2rc2`). The app checks at startup, and **Check for Desktop Updates…** in
the tray menu or the Workbench version badge checks on demand. A newer signed
release asks before doing anything: choose **Download and Install**, **Skip This
Version**, or **Later**. Prerelease builds follow TEST releases while **Use TEST
Updates** is on, and updates never downgrade. Releases without that metadata,
including builds that report **Desktop updates disabled**, upgrade manually: quit
the shell and replace the app (macOS) or run the replacement installer (Windows).
Either path keeps user data under `~/.avibe`; the replacement app verifies and
activates its private Runtime when it opens.

A browser or phone connected to a Runtime that the desktop app manages shows the
app's last completed check in the version badge: the newer version it found, or
that it is up to date. The app owns that result and records it in
`update-status.json` in its application data directory; the browser only
displays it, offers no Upgrade button, and refreshing the badge rereads the
result without starting a new check. Install from the desktop app on the
computer running Avibe. Desktop apps that do not yet record the result keep
showing the general guidance to update from the desktop app.

To remove the private Runtime and Avibe-managed backend installations, choose
**Uninstall Avibe…** in the application's first menu and confirm, then remove
the macOS app or use the Windows system uninstaller. Removing only the app does
not perform that cleanup. Sessions, settings, credentials, and projects under
`~/.avibe` remain available for reinstall; external backend installations are
untouched. Closing the window alone does not stop the Runtime.

Windows installer signing (Authenticode) is tracked in
[#1976](https://github.com/avibe-bot/avibe/issues/1976).
