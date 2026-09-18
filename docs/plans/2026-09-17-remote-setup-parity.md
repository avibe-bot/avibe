# Setup access parity

## Contract

The setup wizard has no local-versus-remote access policy. An authenticated
remote manager follows the same setup completion state and wizard route as a
local manager. This supersedes the remote setup exception introduced in #1162
and the preservation of that exception in the shared Web/desktop design plan.

Remove the remote setup recovery card, its translations, the remote-manager
setup bypass, and their unused types. Reuse the existing wizard, completion
mutation, and setup-boundary revalidation.

## Acceptance

- An unfinished local, personal remote, or organization remote installation
  enters `/setup` and can save completion, then reach the workbench.
- Explicit `/setup` navigation opens the same wizard, including on an already
  configured installation.
- Completed installations keep opening ordinary routes normally.
- Authentication, authorization recovery, non-manager shell access, and
  diagnostic escape routes retain their existing behavior.
- Setup HTTP reads and writes use existing role and CSRF authorization, persist
  completion, and preserve remote pairing.

## Evidence

Use AuthGuard navigation tests with the real Summary completion component,
AppShell route tests, and AUTH-SETUP-405 for the real HTTP/config write boundary.
Run the relevant UI suites, i18n/theme checks, production build, and focused
Python scenario tests. All state and runtime effects must be test-owned.

Live tunnel/browser and local Incus verification remain separate from the
hermetic tests. This change does not require backend authorization changes,
new dependencies, or a new setup flow.

Local verification: the four remote-only UI regressions failed on the old
implementation; after removing both exceptions, 98 focused UI tests and all four
HTTP setup cases passed. The scenario catalog check, UI production build,
TypeScript lint baseline, theme validation, i18n coverage, and changed-file Ruff
check passed.
