# Show Runtime Doctor Diagnostics

## Problem

Show Runtime archive downloads currently collapse HTTP, DNS, TLS, timeout,
permission, and disk failures into `runtime_archive_download_failed`. The
Dependencies page can retry the install, but support cannot tell whether a
retry is useful from that reason alone.

## Design

The implementation now uses the shared managed-dependency network and Doctor
contracts described in `dependency-reliability.md`; Show Runtime is one
dependency adapter rather than a separate retry/error taxonomy.

- Keep `vibe doctor` fast and read-only: inspect the selected provider,
  packaged manifest, platform archive, Node.js compatibility, and installed
  runtime state without making a network request.
- Let `vibe doctor --deep` send a body-free `HEAD` request to the exact selected
  archive URL. Report HTTP status, DNS, TLS, and timeout failures separately.
- Record a redacted structured download error on failed prepare attempts so
  `vibe runtime prepare --json` and Doctor repair results retain the root cause.
- Add the explicit low-risk repair target `vibe doctor repair show-runtime`.
  It prepares the version-pinned archive and reuses verified cache data.
- Do not include Show Runtime in bare `vibe doctor repair`; downloading a
  runtime remains an explicit operation.
- Refuse the legacy implicit URL under
  `avibe-bot/vibe-show-runtime/releases/latest`. Official releases must use the
  manifest bundled with the matching Avibe package.

## Verification

- Unit coverage for structured HTTP/DNS/TLS/timeout failures and redacted URLs.
- Doctor coverage for fast local checks, deep reachability checks, the legacy
  fallback, and explicit repair behavior.
- Focused CLI and Show Runtime tests plus Ruff on changed Python files.
