# Organization Branch Integration Into Master

## Background

The `org` branch contains the Organization authorization, access-control,
management, and remote-session work that must now ship on `master`. The two
branches have continued independently since commit `452ebd43`, so a direct
merge produces broad conflicts across runtime, storage, CLI, UI, tests, and
documentation.

## Goal

Integrate `origin/org` into the latest `origin/master` without regressing the
current `master` architecture or weakening Organization authorization.

## Resolution Contract

- Keep current `master` structure, runtime lifecycle, Model Hub UI, Workbench
  state ownership, and CI/release behavior as the architectural baseline.
- Port Organization authorization, instance/project/resource ACLs, remote
  session revocation, Organization management routes, and compatibility
  normalization from `org` into that baseline.
- Preserve fail-closed behavior for remote callers and missing or stale
  authorization state.
- Do not restore components or contexts deleted by `master`; move required
  Organization behavior to their current replacements.
- Reconcile configuration, migrations, API contracts, i18n, lockfiles, tests,
  and scenario metadata together rather than selecting one branch wholesale.
- Keep the Organization navigation entry hidden while preserving direct
  Organization routes and functionality.

## Verification

- Conflict markers and unmerged index entries are absent.
- Changed Python files pass Ruff.
- Focused authorization, session, remote-access, storage, watches, and CLI
  tests pass.
- Frontend tests, lint, theme validation, and production build pass.
- Parser-backed scenario coverage remains aligned with auth/setup CLI examples.
- GitHub PR CI passes on the integrated head and the current Codex review has
  zero unresolved threads.

## Todo

- [x] Refresh `origin/master` and `origin/org` and record the live divergence.
- [ ] Merge `origin/org` into the integration branch.
- [ ] Resolve conflicts according to the resolution contract.
- [ ] Run focused and broad validation.
- [ ] Push a ready PR targeting `master`.
- [ ] Close CI and Codex review findings on the current PR head.
