# CI Store Preparation

## Scope Decision

The owner's sequence is stability diagnosis, proven repeated preparation, then
multi-run measured shard review. PR #1901 delivered the bounded tool bootstrap
and upgrade-failure evidence changes. Its historical upgrade timeout remains
unexplained; a subsequent passing scenario does not establish its root cause.

This follow-up changes only opt-in test preparation in `tests/conftest.py`,
`tests/test_ci_test_fixtures.py`, `tests/test_resource_access_service.py`,
`tests/test_cli_task_command.py`, `tests/test_update_checker_platforms.py`, and
this plan. No production, workflow, dependency, timeout, selection, or runner
count changes. Do not expand the global AST-based template opt-in heuristic.

## Evidence and Hypothesis

Read-only local profiles on source 16ca51b64's test bodies found:

- Resource access: 72 cases; 57 migration calls consumed 15.1 of 20.7 seconds.
- CLI: 169 cases; 175 migration calls consumed 24.2 of 33.3 seconds. Parser
  construction consumed 2.9 seconds and is not this iteration's target.
- Update checker: 35 cases; 31 migration calls consumed 10.8 of 18.7 seconds.
  Actual settings saves consumed about 0.1 seconds.

Profiled totals are not comparable to unprofiled speedup measurements. PR #1901
CI ran 405 files in 6m53s; ACL/CLI/update checker took 27.03/61.75/41.42 seconds.
Runner contention is not an established cause of differences across runs.

Reuse the existing exclusive private-copy factory with a lazily created,
closed/checkpointed raw Alembic-head template. This differs from the existing
fully imported default-state template: no importer markers or default agents
are pre-seeded. Original store constructors, data migrations, JSON imports,
business calls, and assertions remain real and unchanged.

## Contracts

- Whole schema fingerprint, all initial rows, and persisted SQLite pragmas
  equal a freshly migrated database; copied databases pass integrity checks.
- A template is immutable after publication and each target is an exclusive
  new file under that test's temporary directory. Mutations do not cross copies.
- Only explicit ordinary preparation sites opt in. First-bootstrap tests and
  the resource reader's migration/config persistence boundary remain real.
- Legacy contexts are still seeded and converted by the original data migration;
  replacing their empty schema preparation does not replace the tested conversion.
- All original assertions and selected tests remain; real migration and installer
  gates remain mandatory. No retries, sleeps, shortened histories, or fake clocks.

## Validation and Delivery

Before pushing, compare original assertion ASTs, run the three full consumer
modules, fixture/workflow/shard contracts, real migration and importer coverage,
Ruff and dependency consistency checks. Measure unprofiled before/after locally
and parse every CI file metric and authoritative Finished exit exactly once.
Require exact-head bot PASS, every expected check and matching lint run successful,
zero whole-PR unresolved threads, and CLEAN independent scope/integration before
guarded merge. Verify merged-source master CI separately. Initial review inventory
is zero findings-bearing heads. No shard rebalance from a single noisy sample.

## Local Results

Same private Python environment, unprofiled whole-file pytest runs:

| Consumer | Cases | Original | Prepared |
| --- | ---: | ---: | ---: |
| Resource access | 72 | 21.56s | 2.78s |
| CLI | 169 | 21.22s | 9.24s |
| Update checker | 35 | 13.72s | 7.50s |

These are local samples, not CI speedup claims. The explicit factory covers 26
resource setup sites, 41 direct CLI sites plus two existing shared CLI helpers,
and 31 update-checker setups. CLI's already-created-store path remains untouched;
the factory must never overwrite it. The migration/config persistence test and
the CLI's unseeded first-bootstrap regression remain unchanged.

AST comparison of all 307 original functions, including nested helpers, is equal
after normalizing only the explicit preparation calls and fixture arguments.
All 1,038 assertions, input data, original constructor calls and business calls
are identical. Whole-file validation passed all 276 consumer cases; another
213 fixture/workflow/shard/real-migration/settings cases passed in separate
processes. Ruff 0.4.9, 84-package dependency consistency and whitespace checks
passed. No global environment or production installation was changed.

PR #1901's merged-source run 34006127752 passed all 17 checks, 405 files and
every Finished/metrics record once in 9m20s. Its CLI file took 163.18s but only
33.02s CPU, versus 61.75s/38.90s in the PR run; resource contention is still
unproven. Both real installer suites passed, including 21 distribution cases and
196 installer cases. The historical upgrade timeout remains an explicit residual.
