# Persisted native configuration migration

## Goal and scope

Migrate user-consented, persisted native authentication into Model Hub without
treating the controller's inherited environment or Hub launch overrides as
credentials to import or as blanket blockers. This fixes a reproduced regression
where all three already-Hub backends were disabled in the migration dialog.
No real credentials, native CLI, production-state testing, deployment, or restart
is authorized by this implementation task.

## Boundary contract

- Discovery reads persisted CLI configuration, existing supported native stores,
  and user shell startup files. Runtime authentication environment values are
  neither candidates nor credential suppliers. Environment variables which
  locate configuration directories remain path configuration, not credentials.
- Shell discovery never executes, sources, expands, or evaluates shell code.
  Support literal assignments in `.profile`, `.bash_profile`, `.bash_login`,
  `.bashrc`, `.zshenv`, `.zprofile`, `.zshrc`, and `.zlogin`. Quoted literal
  values, comments, Unicode, and CRLF must preserve their meaning and bytes.
  Dynamic expressions, control flow, command substitutions, includes and
  ambiguous values cannot become guessed credentials; report a specific
  source and remediation, leaving all bytes untouched.
- File references to environment variables resolve only from unambiguous
  persisted literal assignments, never `os.environ`. Unresolved references
  describe a missing persistent value, not a permission failure.
- Every credential keeps its original target and authentication semantics.
  A static bearer token is not assumed to be an API-key header or an OAuth grant.
  Any unsupported transport must have an honest, actionable row-level reason.
- Existing backend custody, authenticated proof, source reuse, cooperative
  lease/drain, durable journal, no-op guards, and recovery remain authoritative.
  Shared shell assignments must not be removed for unconsented consumers.
  Do not replace the takeover state machine or add a parallel journal.
- Consent binds the complete relevant file snapshots, including absent paths
  that could introduce a new grant. Cleanup removes only consented assignment
  lines, preserves unrelated bytes, and uses existing `NativeFileEdit` recovery.
  A changed file, new relevant layer, or ambiguous assignment fails closed
  before exposure. Already exposed grants never roll back to old credentials.
- Public scan adds `source_paths: list[str]` (display-safe file locators, never
  secret content). Existing payload fields remain compatible. Blocked rows
  use specific `settings.models.migration.blocked.*` keys; UI shows every
  distinct source/reason instead of only the first group warning. An empty
  inventory is not represented by a fake unselectable credential.
- Do not loosen actual Keychain permission errors or silently discard an
  unsupported persisted credential to make an entire backend selectable.

## Ownership and validation

Orchestrator owns scanner/planner/lifecycle integration and final delivery.
Independent lanes may own a pure shell parser, UI presentation, and read-only
credential transport audit only after this contract is committed.

Required evidence: fixture reproduction of populated inherited environment
with already-Hub agents; literal shell discovery/reference resolution;
dynamic/refusal cases without execution; exact cleanup/reverse/recovery and
concurrent edits; public/wrong-key rejection with native files preserved;
UI selection and source-specific instructions; focused Python and UI tests,
changed-file Ruff, UI build, then current-head automatic Codex review and CI.
Real-account OAuth/Keychain acceptance remains manual and unperformed.
