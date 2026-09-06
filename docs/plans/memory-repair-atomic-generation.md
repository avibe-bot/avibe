# Atomic Memory Package Repair

## Evidence and Scope Decision

Post-merge CI run 34014938619 passed all 405 Python files and 21 distribution
contracts, but MEMORY-INDEP-026 failed after the released 3.0.13 core upgrade.
The retained repair state reports `memory_package_install_failed`. Concurrent
runtime logs show missing `certifi/cacert.pem` and later missing `vibe.api`.
The installer's stderr was not retained, so its exact original error is unknown.

A private reproduction using that run's core/Memory wheels and uv 0.12.10 proves
the live-environment hazard: concurrent cache writes during `uv tool install
--force` cause environment removal to fail with `Directory not empty`, leaving
the interpreter and application files missing. An ordinary uncontended repair
succeeds. This is consistent with, but does not prove, the historical CI error.

The orchestrator extends the CI repair scope to the existing upgrade planner,
Memory dependency job, their consuming tests (including MEMORY-INDEP-026), and
this plan. Exact-version uv
repairs must reuse the existing staged generation and atomic activation owner.
No new installer, lifecycle state, retry policy, dependency, or timeout is needed.
Pip installations retain their existing installer policy; the proven recursive
tool-environment replacement is a uv-specific failure mechanism.

## Invariants

- Every uv plan installs into a fresh generation, never the running tool root.
- Exact core/Memory versions and recorded release origins remain unchanged.
- Installation or candidate verification failure cannot replace the live launcher.
- Failed candidates are discarded; activated candidates survive restart failure.
- Both service and UI restart from the activated generation. A restart-only retry
  follows the stable launcher without reinstalling or consuming a new source tree.
- Automatic repair admission, bounded attempts, serialized activation, real
  released-wheel upgrades, and all existing CI checks remain intact.

## Validation

- Upgrade planner/lifecycle: 155 passed. Local dependency admission/repair: 194
  passed, including staged install failure, timeout, integrity failure, locked
  launcher, activation success, and restart-only retry contracts.
- A fresh private tool environment with the real same-run wheel pair and uv
  0.12.10 completed staged repair during 289,488 controlled cache writes. All
  protected original files retained their SHA-256 values; the exact core/Memory
  pair passed candidate integrity and launcher activation. No service was started.
- The released-upgrade scenario additionally requires a different active
  interpreter and an intact, still-core-only previous environment.
- Restart/Memory package-shape consumers: 38 passed. Upgrade diagnostics: 6
  passed. Workflow contracts: 60 passed. Ruff 0.4.9, dependency compatibility
  (85 private test-tool packages), and diff whitespace checks passed.
- Pending: all PR checks and exact merged-source
  installer/Windows verification. Docker is unavailable locally; real Docker
  coverage remains mandatory in CI, not replaced by the private uv probe.
