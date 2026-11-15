# Restore the original Desktop update dialog

## Change contract

The sidebar's bottom-right update entry must open the original rc12 update
experience. The account and instance row, account menu, login, and Setup behavior
remain outside this change. No compact update menu is shown.

For a local Workbench, `DesktopAccountRow` mounts the existing `VersionBadge`
with `openUpward`. Its version rows, release link, prerelease preference, check,
close, skip, and upgrade actions retain their existing implementation. The
native-menu event opens that same component.

For a remote Runtime, the entry uses the existing guarded `avibe://updates`
presentation path to the native trusted updater decision. Remote content never
mounts the local updater IPC consumer. The account menu contains no update action.
No native command, authorization boundary, or updater engine changes.

## Source and package comparison

- Base: master `d8187b453f7f272db96ee7ecfe14c7e940c2c4a4`.
- rc12 tag: `642e7b95be3955522f2b716481d40416a78b6788`.
- `VersionBadge.tsx` is byte-identical in rc12, rc16, and this base:
  SHA256 `24e3ec27179e5e6dc0847d323a2fa0e3e3333d56ceeaa629488dea6b2dcad763`.
- The read-only rc12 backup at
  `~/.avibe/backups/avibe-app/20261113-204515/Avibe.app` includes the original
  component in `runtime.zip`; its UI entry asset is `index-C3Ow6JtZ.js`,
  SHA256 `7e03be382bb9a14750ee8b6630013d9742d0cf6e625ce9ce89fc0efb628ce072`.
- The installed rc16 Runtime tree `3.2.6rc16/fb12bffc2009d094` contains
  `index-BY--sGUz.js`,
  SHA256 `dabab09d2918a518d3352c423147a2b50c3e4bb140d10dc98e7436d57ed2edf7`.
  It retains the original component but also includes U1's compact menu.

The regression is the component selected by `DesktopAccountRow`, not a change
inside `VersionBadge`. Reusing the unchanged component restores the original UI.
Its anchor follows the retained bottom-right entry rather than rc12's old
bottom-left badge. The dialog remains inside the viewport.

## Evidence

All fixtures use test-owned state and synthetic API/native replies. The installed
app and backup were read only; no running API, real account, update installation,
service restart, or signing operation was used.

- Four actual-footer regression cases fail on the previous implementation.
  They protect the original dialog/actions, native-menu entry, absence of the
  compact/account-menu update actions, and remote presentation without IPC.
- The repaired footer plus existing VersionBadge suites pass: 61 tests.
- Actual AppShell browser matrix passes: eight cases covering 1200x800 and
  880x600, English/Chinese, light/dark, focus/Escape, account menu, and dialog
  containment (`AUTH-SETUP-178`).
- The actual rc12 backup UI assets were served in a test-owned static fixture:
  four English/Chinese light/dark cases pass. Screenshots confirm matching
  original dialog content, controls, colors, and dimensions; the changed entry
  position is intentional.
- UI and bundled account builds, UI lint, and test/browser TypeScript checks pass.

Logs, screenshots, package hashes, and the standalone rc12 comparison fixture
are retained at `/tmp/restore-update-dialog-sesuzge7suz9p`. The rc12-only fixture
is evidence tooling, not a product or permanent test dependency.

Delivery stops at an English PR with exact-head Codex pass, zero unresolved
threads, required CI green, and CLEAN. Merge, release, installation, and real
platform package acceptance require separate authorization.
