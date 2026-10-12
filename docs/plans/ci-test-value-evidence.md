# CI test-value audit: orchestrator evidence

## Baselines

Both complete successful runs have 536 unique Python file records with exit
codes 0 or 5, matched to the launcher's per-shard file count. Job log REST
endpoints were used because an earlier `gh run view --log` cache contained a
partial in-progress log. Partial data is not used for scheduling.

| Run | Source | Full workflow | Python metrics wall sum (excludes interpreter shutdown) |
| --- | --- | --- | --- |
| 38258683681 | PR head `0edbdf424c5ec1223f1bcebdec63bebdf1a7e82b` | 18m33s | 4206.95s |
| 38261750469 | master `b85e3c567e598eb826c9774c545bd56aad4c3e47` | 18m15s | 4576.83s |

The refreshed snapshot uses the median of each file's two `Finished ... in Ns`
launcher measurements, including interpreter shutdown, not invented costs or
local-machine timings. All 536 files are represented. The old snapshot had
352 entries; it was not a recent complete measurement.

Phase totals are evidence of work, not removal targets: the PR run spent
2731s in test calls, 684s in fixture setup, 392s in collection, and 86s in teardown.
Per-file process overhead is real, but it is not the only bottleneck. Cross-file
stub isolation must not be removed without proving order independence.

## Removed / consolidated CI test constraints

All items below are in `tests/test_ci_pipeline.py`.

| Old test | Why removed | Retained evidence / risk boundary |
| --- | --- | --- |
| `test_artifact_producer_only_installs_the_frontend_and_builds_the_core_distribution` | Pins exact shell argv/installer ordering, rejecting harmless equivalent toolchain changes without testing an artifact. | Production build still required; artifact existence, handoff, package contents and installation are verified by actual build and distribution/installer checks. |
| `test_artifact_build_stops_at_each_failed_boundary` (4 cases) | Replaces Python with a stub and supplies `bash -e` from the test itself; primarily re-tests the shell's exit semantics, not the Actions runner or build backend. | Real artifact production and downstream contract failures remain required; aggregate failure propagation is tested separately. |
| `test_ci_uv_installation_is_exact_and_cache_ownership_is_preserved` | Duplicates the pinned uv version, action SHA, job inventory, command spelling and shared cache implementation. These are performance/tool choices, not product contracts. | Workflow pinning remains unchanged and reviewable; real jobs must successfully install and execute dependencies. No source of truth moved to a test literal. |
| `test_ci_tool_install_command_propagates_failure` (3 cases over every job) | Repeatedly substitutes a stub `python` and checks its exit code under test-supplied `bash -e`; never invokes uv, pip, or a real installation. | Dependency setup remains required in CI; no error suppression added. |
| `test_existing_required_gate_fails_unless_every_dependency_succeeded` | Replace the exhaustive outcome Cartesian product with all-success plus each bad outcome on each individual dependency; the gate checks independent results. | Every dependency is still proven fail-closed for failure, cancellation, skip and missing result. Adding the browser lane requires 13 meaningful probes rather than 125 combinations. |

This deletes four test functions / nine collected cases and redundant gate
combinations, not the watchdog, process hang diagnostics, artifact provenance,
installer, shard discovery or fail-closed aggregate coverage.

## Required UI lane scheduling

Run existing UI validation and existing browser interactions in independent
jobs. The `unit-tests` required context waits for Python shards, `ui-checks`,
and `ui-browser-checks`; any non-success fails the aggregate. Artifact build
and consumers remain independent. No remaining browser suite was demoted or
deleted by this scheduling change. Npm setup is repeated once (~seconds), in
exchange for removing the ~7-8 minute serial dependency on UI validation.

## Measured shard decision

After test/tooling subtraction, retain the isolated-file runner but increase
Python lanes from six to eight. With the refreshed complete launcher timings,
six lanes have a replay lower bound of 12m43s each, before setup; eight replay at
~9m33s before fixture/tooling reductions. Keeping six would require deleting
roughly a quarter of all remaining runtime, which the audit does not justify.
Two extra lightweight Python setups trade modest concurrency for shorter
latency; total PR runner jobs remain below the pre-#2518 Windows-heavy workflow.
Do not confuse that concurrency trade-off with deleting wasted computation.

Train-on-PR / holdout-master replay (independent of the median snapshot): six
lanes reach a 14m43s tail; eight reach 11m20s. Runner variability means these
estimates are not a guarantee of a nine-minute CI. Measure the integrated run
before another concurrency change. Per-file watchdogs, process isolation,
integration exclusions and exactly-once discovery remain intact.

## Initial local validation

- Existing Python 3.12 development environment created with `uv sync --frozen`;
  temporary `ui/dist/index.html` exists only for editable packaging, is ignored,
  and is not a production build or deployable artifact.
- Actionlint 1.7.7 passes `lint.yml` (existing unknown custom runner labels
  excluded from this older actionlint release's label catalog).
- Focused `test_ci_pipeline.py` + `test_ci_unit_test_shards.py`: 62 passed in
  32.82s. This is local correctness evidence, not the full CI performance claim.

- Integrated UI commits `9eacb1c7d` and `a6a6e3e1d` after independently inspecting
  their diffs; focused five-file Vitest run: 408 passed in 2.85s. The consolidated
  `typecheck:tests` and production UI build also pass in the integration worktree.
- The Model Catalog discovery test continues to verify live-fixture isolation
  and discovery of every spec on desktop/mobile. The historical exact count of
  36 and a title prefix are not product contracts: no catalog scenario or label
  was deleted, and the README was aligned. The separate read-only audit's
  conservative concern was evaluated at this consuming boundary, not ignored.

## Integrated validation before first CI

- Master `169c31333` was merged into the task branch; no ownership or migration
  conflicts. Two newly added master test files are discovered normally (the
  measured snapshot remains attributed to its older 536-file source).
- Python lane `47ff8390e` reviewed and integrated: two duplicate cases deleted;
  migrated template copies reused in Vibey/context and Claude recovery tests;
  feedback test-server shutdown polling shortened, not client timeouts.
- CI/shard-focused tests after integration: 63 passed in 27.98s; changed Python
  Ruff 0.4.9 and actionlint pass. UI build/typecheck validation above also passes.
- A grouped local probe reported 75 assertions passed but did not exit before
  its 110-second command deadline. This is **not** counted as a successful
  command and is further reason not to remove file-process isolation here.
  Repeating the same files plus the ledger under the actual CI per-file
  launcher/watchdog semantics completed successfully: context 27, fixture
  contract 12, memory-removal 10, removed-feature contract 26, ledger 75
  (150 tests, all five processes exit 0). Logs/metrics confirm interpreter
  shutdown, not just pytest summaries. No full local backend suite was run.

Performance acceptance and lane-specific deletion inventories are recorded
separately after integration and exact-head CI.
