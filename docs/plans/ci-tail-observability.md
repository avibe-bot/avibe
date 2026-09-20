# CI Tail Observability

## Outcome and sequence

Reduce the long tail before further fixture and artifact-pipeline optimization.
This follows #1879, #1882 and #1886. The initial scope is observational only:
`scripts/ci_unit_tests.sh`, its metrics helper, the consuming pipeline contracts,
and this plan. No new dependency, runner, test selection, timeout, application
behavior, or deployment changes are included.

Three green samples at the end of the previous iteration took 6m24s, 12m40s
and 11m54s (runs 33967285557, 33968815151 and 33969694784). The slowest shard
moved between runners. In the two slow samples, 18 and 19 files respectively
took at least 1.5 times their three-sample median. Total runner occupation grew
only 9-12% relative to the fast sample. CPU contention, I/O, and application
waiting cannot be distinguished from the old elapsed-only logs.

The sequence authorized by the owner is:

1. Collect per-file resource and phase measurements without changing execution.
2. Use those measurements to remove repeated setup where coverage is independent
   of initialization, retaining private databases and real migration/import tests.
3. Shorten the artifact-to-install chain without dropping distribution contracts
   or weakening the existing fail-closed aggregates.

Do not rebalance from a single slow runner, rerun to select a fast result, or
claim a stable improvement from one sample. Later scope decisions need their own
measured justification and consuming tests.

## Measurement contract

The existing launcher still runs one interpreter per file, with its original
300-second stack-dumping watchdog and 20-minute outer job limit. It keeps the
same discovery, integration exclusion, exit handling and subsequent-file
execution after failures. A small pytest plugin observes hooks; it never changes
items, fixtures, reports, assertions or outcomes.

One `CI_TEST_METRICS` JSON record goes to the original diagnostic descriptor
after pytest returns, including failure and empty-selection results. It contains:

- Wall time from the launcher entry, including pytest import and configuration.
- Non-overlapping collection/setup/call/teardown hook durations and invocation
  counts. Parent call timing includes subtests exactly once, not once per report.
- The remaining time outside those hooks, which is not labeled pure import time.
- Cumulative process CPU, block I/O and context switches, separately from waited
  child usage; self peak RSS is in bytes. Children not yet waited are excluded.
- Linux `/proc/self/io` counters and CPU affinity count when available. Unsupported
  or inaccessible optional counters are null, never fabricated zeros. Block I/O
  and proc byte/character counters are different measurements, not interchangeable.
- At most five slow test phases; diagnostic node IDs are capped at 512 characters.

The record explicitly says `pytest_returned_before_interpreter_shutdown`.
It is not proof that a file process exited. The shell's existing `Finished ...`
line and exit code remain authoritative, including interpreter-shutdown hangs.
A timeout during collection/execution may have no metrics record; visible stack
output and the nonzero process exit remain the primary evidence.

CPU/wall ratios and I/O counts provide clues, not direct measurements of CPU
steal time or storage latency. Avoid claiming a host-resource cause from those
counters alone. No host-wide sampler or persistent metrics service is added.

## Verification

Consuming subprocess contracts exercise real phase delays, CPU work in both the
parent and a waited child, bounded subtest summaries, assertion/collection/fixture
failures, empty selection, and all three watchdog boundaries. Optional platform
counters have an explicit unavailable-state test. Existing workflow and shard
contracts remain mandatory. Full exact-head CI must validate every selected file
and all 17 expected checks before integration.

At base `902a97a8824e23d57a184e5c9354cc303db36a78`, local workflow/shard/fixture
contracts passed (38 cases), as did Ruff 0.4.9, dependency consistency and shell
syntax checks. The real delivery-state-machine suite passed all 177 tests with
the observer: 16.30s launcher wall, 6.78s collection, 4.09s setup, 4.71s call,
0.38s teardown. These macOS values are not predictions for hosted Linux runners.
Four alternating small-suite measurements had medians of 0.757s without and
0.762s with the observer. This is a coarse overhead check, not a speedup claim;
the machine was not reserved for benchmarking.

The implementation follows pytest's public wrapper hooks and Python's resource
accounting APIs: [pytest hook reference](https://docs.pytest.org/en/stable/reference/reference.html#hooks),
[resource accounting](https://docs.python.org/3.12/library/resource.html).

## Measured setup reduction

Head `bc1970b2c8` passed the head-bound automatic review (reaction 490355915),
with zero complete reviews or threads. Run 33973069748 passed all 17 checks in
6m57s. All 399 discovered files ran exactly once, with successful shell exits
and one metrics record each. Six shard jobs took 5m28s-6m33s; summed file wall
time was 1999.63s versus 1658.03s parent plus waited-child CPU. This sample did
not reproduce the earlier extreme runner skew and cannot explain it retroactively.

Two existing fixtures are independently dominated by repeated initialization:

| Suite | File wall | Setup | Call | Recorded write bytes |
| --- | ---: | ---: | ---: | ---: |
| Delivery FSM | 33.28s | 19.44s | 10.37s | 571 MB |
| Harness definition lifecycle | 29.11s | 26.02s | 2.02s | 282 MB |

The orchestrator extends scope to these two test modules, `tests/conftest.py`
and `tests/test_ci_test_fixtures.py`. Reuse the existing private-copy factory
with an explicit module-owned template, without changing its default migrated
template. Build each template with the fixture's original initializer, close
its engines and checkpoint the WAL before any copy. FSM retains metadata-only
empty tables, with its original per-test session seed. Harness retains exactly
the schema/data from the explicit-path background-store constructor. Neither
may inherit the default importer's markers or seeded rows.

Contracts compare each template's complete schema, rows and persistent pragmas
with a fresh real initialization, and verify private-copy identity, mutation isolation,
no-overwrite and path confinement. Original test bodies and assertions remain
unchanged; all migration and legacy-import suites continue real initialization.
This is a bounded fixture change, not a production database optimization.

The initial equivalence check exposed nondeterministic constraint/index emission
order across real Alembic rebuilds. Harness therefore reuses the existing migration
suite's whole-schema fingerprint (including constraints and expression indexes),
plus all dumped rows, rather than treating DDL order as behavior. FSM's direct
metadata initializer remains byte-for-byte SQL-dump comparable.

Local validation passed 263 fixture/FSM/lifecycle cases and 169
workflow/shard/fixture/real-migration cases. All 217 original non-fixture
functions/classes in the two business modules are AST-identical. Ruff 0.4.9
and dependency consistency passed. Observer measurements after the change:
FSM 178 cases, 5.91s wall/1.00s setup; lifecycle 74 cases, 2.21s wall/0.51s
setup. These include the two new equivalence contracts and are local samples,
not hosted-CI speedup estimates. The lifecycle baseline profile independently
attributed 14.05s to 41 real migration calls out of 15.88s profiled execution.

Hosted step-two run 33974210485 on `925722ae9` passed all 17 checks in 7m22s,
again with all 399 files, successful shell exits and metrics exactly once.
FSM fell from 33.28s to 15.11s, setup 19.44s to 1.78s, recorded writes 571MB
to 175MB. Lifecycle fell from 29.11s to 4.97s, setup 26.02s to 1.06s, writes
282MB to 58MB. Their original call-phase work remains exercised, with one new
equivalence test each. The unchanged shard 4 rose from 393s to 427s, while its
summed CPU fell from 337s to 263s. This is not evidence of a faster whole run
or proof of a particular host bottleneck; the two fixture reductions are the
demonstrated result. Automatic review completed with new reaction 490371521
on the unchanged head; complete review/thread collections were empty.

## Artifact-chain scope decision

Run 33973069748 spent 99.55s in the 21 distribution contracts before uploading
artifacts. The producer took 218s, then the installer/Memory/Windows consumers
took 123s/149s/176s. This is independent validation work, not a build prerequisite.

Extend scope only to `.github/workflows/lint.yml` and the existing pipeline
contracts. Move the unchanged distribution suite into the existing installer
matrix member, still under the required fail-closed install aggregate. Do not
add runners or combine mutable environments. Keep the producer's exact core
wheel artifact unchanged; publish an additional same-run artifact containing
both sdists and the Memory wheel with their directory layout preserved. Only
the installer member downloads that companion artifact. Distribution tests
continue to use the exact producer wheel pair and explicit contract version,
with all real isolated installs and sdist resolution tests intact. Any skipped
contract or nonzero pytest result must fail the job and existing aggregate.

This trades approximately 100s of all-consumer waiting for approximately 100s
of work on the shorter installer branch; it is not a promise of 100s saved on
the whole workflow. The old timestamps suggest about 50s less artifact-chain
tail, subject to download/setup/runner variance. Only a complete new green run
can supply an actual result. Real migration tests remain unchanged: repeated
reference construction is not this iteration's scope.

The companion layout follows the artifact action's documented
[multiple-path common-ancestor behavior](https://github.com/actions/upload-artifact#upload-using-multiple-paths-and-exclusions).

Local workflow/fixture/shard/source-package validation passed 59 cases. Actual
core and Memory wheels plus sdists were built at contract version 3.0.99rc1;
all 21 unchanged distribution cases passed across the initial 20 successes and
one targeted recovery after provisioning missing `pip` in the private test venv.
The original failure occurred before sdist resolution because `uv venv` does
not seed pip; hosted setup-python already provides it. No package/runtime
dependency was added. The local wheels use the unit-test UI placeholder, while
the unchanged hosted production UI build and exact same-run artifact transfer
still require full new-head CI. Ruff 0.4.9 and dependency consistency passed.

Before integration, master advanced to `a8145d683b302a1d5411db8e97bf3db5d462ebe1`
through #1888. The orchestrator inspected its API change and consuming dependency
tests: no overlap with this PR's nine-file scope, no allocated artifact-name
collision. Integrate that source and run its new dependency-consumer tests with
the changed fixture/workflow contracts; the new file uses the existing shard
planner fallback, with no invented timing entry.

After merging that exact master, all 561 dependency-consumer/FSM/lifecycle/
workflow/shard/fixture cases passed. The new inventory is 400 files; the previous
399-file hosted comparisons remain pinned to their original heads. The final
scope remains nine files relative to the integrated master. The prior automatic
review was terminal before the next push (live bot +1, no reviews or threads);
the next head requires its own complete review and CI evidence before merge.
