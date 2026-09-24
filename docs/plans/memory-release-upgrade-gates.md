# Memory retirement: official upgrade gates

## Background and goal

The removed Memory implementation must not reappear in persisted configuration,
but historical user data, released updater compatibility, and published Runtime
recovery assets remain supported. A normal startup and the official release path
are separate gates: an explicit config save and an opt-in local E2E do not cover
either one by themselves.

## Delivery

- MUC-001/002/003: on successful startup migration, remove only the obsolete
  root `memory` key; preserve every other root key and all Memory user files.
  A memory-only write preserves even a minimal valid Model Hub payload exactly;
  a real Model Hub migration retains its existing serialization behavior.
  Recovery and losing a config compare-and-swap leave the old file intact.
- MUC-004: before GitHub asset upload and PyPI publication, download both
  published v3.1.0 wheels, verify their fixed SHA-256 identities, then run the
  released updater through real pip and uv in disposable Docker against the
  newly built core and inert bridge wheels. Missing inputs or Docker fail the
  release; the normal local opt-in test still skips without release inputs.
  Package wheels come from the target tag; the gate and all of its helpers come
  from the running workflow revision, including on older-tag dispatches. A
  target at or before v3.1.0 has no forward retirement upgrade to exercise.
- MUC-005: the historical Runtime backup guard selects only non-draft GitHub
  releases. Published prereleases with self-pinned manifests are included; the
  existing inert bridge policy and invalid-manifest exclusions still apply.

## Evidence and residual checks

Unit/contract: config migration, concurrent writer/retry, recovery, workflow
ordering and fail-closed prerequisites, and published/draft/prerelease guard
selection. Scenario: stable IDs in `tests/scenarios/memory_upgrade_compatibility/`,
with the real four-case updater test gated at official release build time.
Manually confirm a future official release's four Docker upgrade cases and
GitHub assets before accepting its PyPI publication. No host service restart,
Memory data deletion, or regression-state reset is part of this change.

## Known by design

- The compatibility bridge remains an inert GitHub Release wheel, not a Memory
  runtime or new PyPI companion. Legacy CLI argv and delivery metadata remain
  unchanged.
- Every target version strictly newer than v3.1.0, including newer prereleases
  and postreleases, must pass the real v3.1.0-to-target pip/uv upgrade gate.
  Earlier or equal versions have no forward upgrade from v3.1.0 and cannot
  exercise that scenario; their existing installer regressions still run.
- Published historical GitHub release assets are byte-immutable. A dispatch
  replay of an already published v3.1.0 cannot replace its real Memory wheel
  with an inert bridge: the existing asset comparison must reject that attempt.
- The release-only historical Runtime guard continues backing up published
  manifests; it does not install, upgrade, or roll back a Memory package.
- The old user-owned Memory directories and their contents are left untouched.
