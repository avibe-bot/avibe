# Show Runtime Verified Repair and Status

## Background

At `origin/master` commit `1aae3e0e02c1ae30391a047886480032269fa37f`,
the Settings dependency job and Doctor did not share one repair contract:

- Settings called `ShowRuntimeManager.prepare(force=True)` directly. A healthy
  managed runtime could therefore be replaced without first observing a start
  failure.
- Doctor privately implemented isolated startability verification before and
  after installation. That policy was unavailable to other callers.
- `dependencies_status()` caught every exception raised while inspecting Show
  Runtime and fabricated `install.state=absent`. Settings consequently exposed
  an absence-based install action when the actual state was unknown.

The managed manifest installer already staged downloads before publishing its
`current.json` pointer, but Show Runtime selected destructive same-identity
replacement on force. Direct archive and npm compatibility providers also
installed into mutable fixed paths. Cleanup did not retain paths referenced by
cached commands or live managers.

## Goal

Make `ShowRuntimeManager` the single owner of authoritative status and verified
repair for both Settings and Doctor. Repair must mutate an installation only
after an observed start failure. Inspection or verification uncertainty must be
non-destructive, and a replacement must start successfully before publication.

## Contract

1. `status()` converts expected operational inspection failures into structured
   `install.state=failed` evidence. Programming defects still propagate.
2. `repair()` verifies an installed command in an isolated workspace. A healthy
   runtime is returned unchanged; an undetermined check never installs; only an
   observed start failure authorizes replacement.
3. Candidate verification runs before pointer publication. Manifest, archive,
   and npm replacements use immutable generation directories so a failed
   candidate leaves the prior installation available.
4. Commands returned by a manager retain their generation. Cleanup preserves
   current, live, and cached references across identities while applying the
   existing rollback budget only to unreferenced generations.
5. Settings keeps its asynchronous job and polling surface, and Doctor remains
   a presentation adapter. Neither caller reimplements verification or status
   projection semantics.

## Validation

- Shared manager tests cover pre-publication validation and failure vocabulary.
- Show Runtime tests cover healthy, failed, and undetermined verification;
  immutable failed replacement; pointer preservation; cached/live references;
  and identity-specific cleanup.
- API and CLI tests exercise both callers through the same real manager owner.
- Settings API and component tests distinguish structured inspection failure
  from true absence, preserve evidence, and suppress unsafe actions.
- Focused backend tests, changed-file Ruff checks, UI tests, UI build, exact-head
  Codex review, and required GitHub Actions checks gate delivery.
