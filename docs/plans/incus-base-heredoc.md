# Incus base-image backend heredoc

Issue #1999 / INCUS-BOOTSTRAP-001: Python expands the npm-prefix newline
before `textwrap.dedent`, leaving the backend heredoc terminator indented.
Fresh base-image provisioning then exits 127 before publication.

Preserve the literal shell escape in the Python string so dedenting restores
the heredoc boundary. Keep the installer order, service-user invocation,
`set -euo pipefail`, and publication lifecycle unchanged.

Acceptance invariants:

- The generated recipe executes successfully and returns from the backend
  shell to the outer bootstrap exactly once.
- The service user's `.npmrc` contains one newline-terminated prefix, including
  a temporary home path containing spaces and non-ASCII characters.
- npm and installer-download failures propagate their exit codes and prevent
  the remaining bootstrap, cleanup, stop, image deletion, and publication.

Validation executes the generated body with a non-login Bash, a minimal
environment, test-owned homes, bounded execution, and a PATH limited to stubs
and selected shell/filesystem utilities. It exercises the real Python Runner's
failure propagation while replacing the Incus subprocess boundary. It does not
install packages, create users, or publish an image. The existing Incus runner
suite and changed-file Ruff remain required.

Independent Tester acceptance and PM's draft PR delivery are separate gates.
This focused regression does not replace a real Incus image build or full
product acceptance. Keep the Issue open until merge and mainline acceptance.
