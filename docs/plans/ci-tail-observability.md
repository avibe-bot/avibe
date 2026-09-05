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
