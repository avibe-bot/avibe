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

The initial `82b3be73b` snapshot used the median of each file's two
`Finished ... in Ns` launcher measurements, including interpreter shutdown,
not invented costs or local-machine timings. All 536 source files were
represented; the follow-up below refreshes it from the completed 538-file run.
The original stale snapshot had 352 entries.

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

## First exact-head result and follow-up

PR #2527 head `82b3be73b` passed full `lint` run **38285598210** and received a
new head-bound Codex +1 without findings. All 538 Python files have complete
metrics and successful exits (0 or 5). Results:

| Measure | #2518 baseline | First #2527 run |
| --- | --- | --- |
| Full PR wall time | 18m33s | **14m21s** |
| Longest Python job | 16m33s | **10m27s** |
| UI base + browsers | 16m45s serial | **6m51s / 8m40s parallel** |
| UI Vitest | 342s | **291.71s** |
| Test TypeScript check | 72s | **~31s** |
| Total runner time | 108m17s | **114m41s** |

The latency target is not met. Total runner consumption did not drop in this
sample: Windows slowed and other/master test work changed; do not attribute
all elapsed reduction to deleting computation. The required Windows smoke
now defines the tail: 10m41s execution after a 1m31s artifact build; it starts
3m34s after workflow creation. Python job starts range from +3s to +150s.
The no-queue dependency path is about 12m16s, not the observed 14m21s.

The finished-file workload totals 4596s, so eight lanes have an ~9m35s lower
bound before setup even if perfectly balanced. Follow-up uses **nine** lanes
(~8m31s replay each) and refreshes all 538 snapshot entries from this complete
run. This is one additional setup, not an unbounded runner increase.

### Remove repeated Windows installations, not installation assertions

The smoke previously installed six times: initial install, three identical
same-wheel replacements, another install just to set up uninstall peers, and
one required reinstall after removal. It now installs **three** times:

1. Initial install, then one real replacement to verify generation retirement.
2. Add managed/foreign peers to that already-verified installation; run default
   uninstall and verify data preservation / only owned launcher removal.
3. Reinstall once after removal; test refusal without purge consent, then
   confirmed purge and script-block session survival.

All unique assertions remain; repeating the identical replacement transition
three times is not a distinct contract. This removes three expensive install
passes (each was roughly a minute in the measured run). No installer production
code or network/runtime-preparation behavior is bypassed.

Inspection also found the uninstall step's reinstall did not inherit the first
step's `AVIBE_INSTALL_PACKAGE_SPEC`, so it could install published PyPI code
instead of the PR artifact. The step now explicitly selects the downloaded
wheel, preserving exact-head artifact provenance.

### Shared feedback fixture shutdown

The recovery file's HTTP fixture was already faster, but its imported fake
GitHub fixture still waited on the stdlib half-second shutdown polling. Apply
the same 0.01s server polling to that shared fixture. Real request timeouts,
barriers and recovery assertions are unchanged. Focused per-file reruns:
56 feedback tests pass (teardown total 0.77s) and 43 recovery tests pass
(teardown 0.56s); the two actual stall calls still last ~10.3s each.

Follow-up validation: 63 CI/shard contract tests pass, changed Python Ruff and
actionlint pass. Windows PowerShell is not available on this macOS host; the
real required Windows CI job is the acceptance test for its fixture change.

Performance acceptance remains pending on the next exact-head full run.
