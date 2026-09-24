

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
records the embedded archive and toolchain hashes. `SIGNATURE` is descriptive
metadata, **not a cryptographic signature file**. Hashes detect changed downloads;
they do not establish a trusted developer identity.

The macOS **app inside the DMG is ad-hoc signed**. The outer DMG is unsigned and
unnotarized; this is not Developer ID distribution. Open the DMG, drag Avibe to
Applications, and launch it. If macOS blocks this verified test app, use System
Settings → Privacy & Security → Open Anyway for this app and confirm the prompt.
The Windows NSIS installer is unsigned and may show SmartScreen or an unknown
publisher prompt. After checking the download, use More info → Run anyway only
if your machine's policy permits it. Keep Gatekeeper, SmartScreen, and antivirus
enabled; managed machines may require administrator approval.

Python, Avibe, Node, and npm are bundled; no separate installation is required.
Agent backends are detected or installed when selected in Avibe. There is no
desktop auto-updater. Quit the shell and manually replace the app (macOS) or run
the replacement installer (Windows) to upgrade. User data under `~/.avibe` is
retained; reopening the replacement app verifies and activates its private Runtime.

To remove the private Runtime and Avibe-managed backend installations, choose
**Uninstall Avibe…** in the application's first menu and confirm, then remove
the macOS app or use the Windows system uninstaller. Removing only the app does
not perform that cleanup. Sessions, settings, credentials, and projects under
`~/.avibe` remain available for reinstall; external backend installations are
untouched. Closing the window alone does not stop the Runtime.

Trusted distribution, including the remaining signing layers, is tracked in
[#1976](https://github.com/avibe-bot/avibe/issues/1976).
