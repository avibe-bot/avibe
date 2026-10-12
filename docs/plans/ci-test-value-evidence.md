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

## Initial local validation

- Existing Python 3.12 development environment created with `uv sync --frozen`;
  temporary `ui/dist/index.html` exists only for editable packaging, is ignored,
  and is not a production build or deployable artifact.
- Actionlint 1.7.7 passes `lint.yml` (existing unknown custom runner labels
  excluded from this older actionlint release's label catalog).
- Focused `test_ci_pipeline.py` + `test_ci_unit_test_shards.py`: 62 passed in
  32.82s. This is local correctness evidence, not the full CI performance claim.

Performance acceptance and lane-specific deletion inventories are recorded
separately after integration and exact-head CI.
