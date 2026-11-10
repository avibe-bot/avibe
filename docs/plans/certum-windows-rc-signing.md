# Certum-signed Windows RC delivery

## Contract

The next `gh-vX.Y.ZrcN` desktop release signs the final NSIS installer with the
pinned `ssign` 0.1.9 Windows binary and Certum SimplySign, then validates the
publisher, code-signing EKU, RFC 3161 timestamp, payload preservation, and
negative tamper case before recording release hashes. If the test updater
channel is enabled, the Tauri updater signature is produced in a separate
`desktop-updater` job from those already Authenticode-signed bytes.

## Boundaries

- `windows-signing` is used only by the Certum signing job. Its secrets are
  mapped to `CERTUM_EMAIL` and `CERTUM_OTP` for one child invocation.
- The signer output is captured in memory, and its Windows bearer-token cache is
  blocked by setting `XDG_RUNTIME_DIR` to a regular file. No credential
  directory is uploaded or cached.
- Build and Certum signing are separate jobs. Tauri updater signing is a third
  job when enabled, retaining its existing `desktop-updater` environment.
- Only the outer NSIS installer is Authenticode-signed in this first RC. The
  embedded application and bundled third-party binaries are not claimed to be
  independently signed.
- Stable `v*` releases use the same Certum-signed Windows installer path as RC
  `gh-v*` releases. Manual unsigned builds retain their existing path; this
  migration applies to future stable releases and does not retrofit a release
  that has already been published.

## Owner approval

The owner approved migrating stable `v*` releases to Certum Authenticode
signing on 2026-11-10 at 01:53 (UTC+08:00).

## Acceptance

A release is not final until the public Windows installer has been downloaded
from the new GitHub-only RC or stable release and independently checked for
Authenticode validity, expected publisher and timestamp, final public hash,
updater manifest (when enabled), and tamper rejection.
