# Converge Memory's native artifact during Wake

## Background and contract

A core upgrade can correctly install its exact GitHub Memory companion while
leaving a healthy native artifact from the previous release active. Startup
already calls the controller-owned, non-destructive `MemoryRuntime.wake`.
That path repairs missing/invalid artifacts but overlooks a verified artifact
whose bytes or contract no longer match the new packaged manifest.

Extend this existing owner, not the package updater or a second background
installer. Startup, explicit Wake, and bounded crash recovery must share the
same decision. A proven manifest mismatch is actionable even when the EverOS
version is unchanged. An unchanged/unknown manifest or explicit development
runtime does not create a new update requirement.

## Invariants

- Hold the existing operation lease; pause and join writers and prove the old
  sidecar stopped before installing. Keep the explicit running-runtime install
  rejection unchanged.
- Use the existing manifest/hash/extraction/cold-admission/root-compatibility/
  activation bridge. Never write the user's `enabled` preference, delete data,
  grant accepted-loss authority, or add package rollback/recovery machinery.
- If installation fails, resume only the exact previously admitted active
  artifact when its binary and fingerprint are unchanged and its root/readiness
  checks still pass. Retain the install failure in the dependency owner and
  expose it in the Wake result/log; running the old artifact is not upgrade
  success. An invalid old artifact cannot provide fallback authority.
- No new timer, persistent workflow, retries within a Wake, or service restart.
  A later startup/explicit Wake or existing bounded crash recovery may retry.
- Cancellation/shutdown retains the existing install-join and lease semantics.
  Conflicting settings/repair/install cannot overlap the operation.

## Evidence and scope

Add `MEMORY-RUNTIME-INSTALL-004/005` scenarios with a real managed artifact
manager, generated archives, actual active-pointer and provider-root checks,
and deterministic process/provider boundaries. Cover same-version new bytes,
unchanged state, failed download/admission/activation, incompatible nonempty
roots, configuration/data preservation, contention and cancellation.
Existing wake, artifact, controller and package-consumer suites remain gates.

Hermetic tests do not prove a new released archive or actual host upgrade.
No release, deployment, production restart, Incus mutation, or merge is
authorized by this source-fix task. Deliver a reviewed PR with full CI first.

## Progress

- [x] Inspect actual rc2 receipts and end-to-end production call path.
- [x] Implement the smallest Wake policy change and deterministic regression.
- [x] Run focused/broader tests and changed-file Ruff.
- [ ] Independent root review, exact-head Codex review, complete lint CI.

## Initial local validation

- The selected runtime, supervisor, artifact, disabled-isolation, dependency,
  upgrade, distribution and internal-server suites: 968 passed, 11 skipped.
  One skip needs pinned real EverOS; ten need the CI-built core/Memory package
  matrix. Their dedicated CI jobs remain required, not presumed successful.
- Nine new real-manager scenarios cover startup/running Wake, changed bytes
  or contracts at the same version, and five failed-update boundaries. Narrow
  contracts additionally cover unverified/changed fallback, proven stop,
  unchanged/unknown manifests and cancellation retaining the operation lease.
- The unmodified base Wake method fails all seven initial regression scenarios
  when substituted in-process; the worktree source is not changed by that probe.
- Changed-file Ruff, whitespace checks and the UI build pass. The UI build was
  needed for the normal task-local editable package bootstrap, not UI changes.
  Its existing dependency audit reports 14 issues (6 high); dependency/security
  remediation is not part of this change.
- Initial test-environment failures and stricter fixture assertion corrections
  were diagnosed and retained before the final clean run. No host service,
  configuration, released archive or Incus environment was modified.

`Wake.ok` continues to mean runtime availability. If the selected update fails
but the identical admitted old artifact passes root/readiness checks, Wake also
returns `artifact_update` with the unsuccessful install result. Dependency
status retains the reason and manifest mismatch; this is not upgrade success.

## Review boundary corrections

Round 1 reviewed one findings-bearing head and three distinct classes:
pre-install lifecycle compensation, public failure propagation, and selected
manifest authority. No repeated class or circuit-breaker threshold was reached.

- Restore capture intake on a pre-install abort only when the original admitted
  sidecar is still running/current. A pending writer close retains its own fence
  until cleanup ends; incomplete stop and shutdown remain closed. Record abort
  reasons in status rather than leaving an apparently healthy paused writer.
- Compute `matches_manifest` only for installable selections. Unavailable,
  malformed, unsupported or incompatible-build source manifests are unknown,
  not update authority. This refines the existing producer field, adding no flag.
- The Web Wake response retains only bounded `artifact_update.ok/reason`
  information. Both settings consumers show a localized failed-update message
  even when the prior engine remains available. Raw download diagnostics are
  not exposed. Supervisor recovery keeps its original availability semantics.

### Round 1 validation

- Expanded Python selection, including the Web response consumer: 1011 passed,
  11 skipped, zero failures/errors. The same pinned-EverOS and CI-built package
  prerequisites account for all skips; they remain explicit CI gates.
- 66 UI tests pass across both settings consumers and their shared Memory
  helper suite. Test typechecking, baseline-aware UI lint, UI build,
  changed-file Ruff and whitespace checks also pass.
- Added eight rejected-manifest variants, three pre-install abort variants
  with actual writer admission checks, a public HTTP response contract, and
  both settings-page failed-update toast consumers.
- Released-native unattended host upgrade and real search are not exercised
  by these isolated source tests. No deployment or publication is authorized.
