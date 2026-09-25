# Desktop signed update contract

## Outcome and boundaries

From baseline `2a8ce221283f81cdd37cf375c92a977669a3b540`, add one native desktop
update owner. Startup checks only; installation requires a native confirmation.
The Web version entry routes to that owner without receiving updater IPC powers.
Current version comes from Tauri package metadata stamped by the release build.
Stable builds default to stable; prerelease builds default to TEST. Channel choice
is local to the shell. No downgrade, Python in-place upgrade, backend update, or
mutation of `~/.avibe` / private Runtime slots is part of application replacement.

## Distribution contract

GitHub Releases in `avibe-bot/avibe` are the discovery source. Canonical stable
`vX.Y.Z` and TEST `gh-vX.Y.ZrcN` tags select disjoint channels. Each target owns a
Tauri-compatible manifest plus detached Minisign signature. The signed manifest
binds repository, tag, peeled source commit, SemVer, target, artifact URL, size,
SHA-256, and the artifact's own Tauri signature. The shell verifies all these
fields and the GitHub tag source before offering installation. It checks the
plugin's fetched JSON against the authenticated manifest before downloading.

macOS updater payloads are `.app.tar.gz`; DMGs remain manual installers. Windows
uses NSIS EXE. `.SIGNATURE` remains human-readable OS-signing metadata and never
counts as an updater signature. Missing signing configuration disables update
publication; an enabled channel with partial configuration fails the workflow.
Existing unsigned releases cannot become auto-installable retroactively.

## Installation and verification

The pinned Tauri v2 updater owns HTTPS update checks and artifact signature
verification. Application replacement must preserve the old runnable application
on failure. The implementation must account for the upstream macOS install path
not restoring its temporary backup, and Windows returning before NSIS completes.
Validate metadata/signatures and all bytes before any replacement operation.

Use shared Rust contract verification both in the shell and release validation;
fixture tests cover source/version/target mismatches, tampering, and failure
preservation. Run focused Python workflow tests, changed-file Ruff, desktop
localization/build, Rust fmt/check/test, and the shared Web build. Native signed
upgrade acceptance on macOS arm64/x64 and Windows x64 remains an explicit release
operator check; never simulate success by updating this machine's installation.

## Local evidence (2026-09-25)

- 239 Rust tests pass, including real Tauri Minisign fixtures for both channels
  and all three targets, official plugin manifest consumption, arbitrary-byte
  metadata tampering, download/install gate failures, and macOS restore failure.
- 57 focused Python release tests pass, including actual workflow shell fixtures
  and the real Rust verifier executed under test-owned HOME/state.
- Desktop and Workbench builds, desktop locale parity, focused VersionBadge tests,
  Rust formatting/check/Clippy, and changed-file Ruff pass.
- Windows process/rollback fixtures are wired to the existing Windows CI runner.
- No real updater secret was used, tag created, release published, or app replaced.
  Native signed two-version upgrade acceptance remains a release prerequisite.

## First review follow-up

Head `65ff58a2e701df861ddc6a1b0cedd9cfb676c51b` received one Codex finding:
Windows recovery merged the backup into a partial installation. Recovery now
removes the failed installation before copying the intact backup; native fixtures
cover deleted installations, newly introduced DLLs/resources, and a successful
installer exit followed by a rejected version. Native execution remains a
Windows CI gate.

Signed fixture files disable Git text conversion. All 14 tracked fixture files
retain their exact signed bytes under a `core.autocrlf=true` checkout, and all
five Rust updater contract tests pass. Release job condition fixtures now model
resolution results and the desktop enable flag independently, including stable
and TEST failures; the three focused Python release files pass 1,653 tests.
