# CI Media and Nested Store Preparation

## Scope and Evidence

This is the second phase after wait attribution (#1909) and the independent
atomic Memory repair (#1910). It changes only explicit preparation in
`tests/test_workbench_media.py` and `tests/test_harness_failure_visibility.py`.
No application, workflow, dependency, timeout, runner, shard, or selection change.

The media profile passed 28 cases: 25 migration calls from 18 source callsites
took 8.302 seconds of 19.127 profiled seconds. The current unprofiled local
baseline passed in 7.21 seconds, demonstrating why profile and wall timings
cannot be compared as a speedup. The complete green master run 34017192558
took 408 seconds; media consumed 19.91 seconds, 17.30 CPU seconds, and 174 MB
of Linux write bytes.

The malformed-metadata Harness writer test initializes seven nested stores
outside its existing fixed-path fixture. A three-test profile attributed 2.728
seconds to eight migrations. The unmodified full module passed 225 cases in
18.51 seconds locally; the nested test's call took 1.63 seconds. Existing
query-history batching is not changed.

## Invariants

- Use the existing lazy, closed/checkpointed raw migration schema through
  `sqlite_schema_db_factory`. Each test or nested writer owns an exclusive
  private copy; no imported JSON state or default Agent markers are preseeded.
- All original media registration, HTTP download, legacy filename/row,
  authorization, rewrite, and failure assertions remain. These cases test
  legacy media data behavior, not historical schema upgrades.
- Every nested Harness store still calls its real constructor and original
  serializers, claims, terminal writers, and raw metadata reads. Only empty
  database preparation moves before construction.
- The factory's existing whole-schema, all-row, pragma, integrity, mutation
  isolation, no-overwrite, and path-boundary contracts remain the proof of
  equivalent preparation. Historical migrations and importer/locking tests
  retain their real paths. Global autouse selection is unchanged.
- Keep one Python process per file, the 300-second visible-stack watchdog,
  the 20-minute outer bound, all 17 CI checks, and fail-closed aggregates.

## Installation-Chain Review

The remaining chain has independent value. In run 34013888907 the existing
installer job took 216 seconds: distribution contracts consumed 99.17 seconds
and installation/upgrade cases 97.15 seconds. Fresh Docker installation and
released-wheel upgrade took 37.17 and 44.03 seconds. The distribution suite's
six private virtual environments verify actual wheel/sdist installation,
dependency resolution, exact peer versions, and core-only isolation.

The producer already uses a minimal isolated builder. Reusing mutable installed
environments or bypassing dependencies would remove the behavior these tests
exercise. No further installation-chain patch is selected without an independently
measured, contract-preserving benefit. After the repair, master run 34017192558
spent 70 seconds producing artifacts and 315 seconds from producer start through
the installation aggregate. Distribution and installation tests took 99.68 and
109.23 seconds, respectively; the Python tail, not installation, set the 408-second
workflow duration. The orchestrator's phase-three decision is no further
installation change: retain the independently valuable isolation and real upgrade
coverage. Recheck final successful PR/master timestamps before delivery. Keep six
shards unchanged.

## Validation

All 460 local cases passed in separate file processes: media 28, Harness 225,
private-fixture contracts 12, workflow 60, shard 6, and real migrations 129.
The initial media run took 10.86 seconds, including 8.88 seconds in the first
UI-module-consuming call on the fresh environment; no wall-time improvement is
claimed from that comparison. A subsequent read-only profile passed all 28 cases
in 2.55 seconds and recorded exactly one raw migration (0.359 seconds), rather
than 25. This verifies removed repeated work, not a hosted-CI speedup.
Harness passed in 17.25 seconds versus the 18.51-second local baseline.

AST comparison retained all 291 original definitions and 1,227 assertions after
normalizing only the 19 explicit fixture signatures/preparation sites. Ruff 0.4.9,
dependency consistency (84 packages), and whitespace checks passed. Initial
editable dependency bootstrap began before the ignored UI placeholder existed;
after creating that existing workflow prerequisite, installation succeeded.
No production UI or environment was changed.

Before the first PR push, inspect and integrate latest master #1911 (073d277fe),
whose Model Hub route normalization and consuming config tests do not overlap
this scope. Repeat consumers after integration. Require current-head bot PASS,
complete review/thread inventory, every exact-head lint run successful, and
post-merge source CI. Compare complete first-attempt green file metrics; failed
runs and local profile values are not CI performance samples.
