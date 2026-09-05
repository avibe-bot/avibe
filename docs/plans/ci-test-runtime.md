# CI Test Runtime

## Goal and invariants

Reduce mandatory CI wall time and sensitivity to slow runners without removing
test coverage, weakening failure propagation, or sharing mutable test state.
This continues the critical-path work in #1879. No application deployment,
release, running service, or remote regression environment is in scope.

- Every existing unit test remains selected exactly once across the shards.
- Test files continue to run in independent Python processes.
- Each test retains its own home, database, and mutable resources.
- Migration/import tests still exercise real initialization from unseeded state.
- All existing UI, packaging, migration, Windows, and installation gates remain
  mandatory; a missing or non-successful dependency cannot become a green gate.
- Performance claims distinguish CI measurements from local profiles and estimates.

## Diagnosis

At source `928924dd82bb48c7eea67ff06ddd844d442cb2a1`, master CI run
33956553276 passed but took 17m38s. The equivalent PR run 33955817335 took
8m26s. The slow shard spent 545s in Harness failure visibility tests versus
232s in the PR run; several other files on that same runner slowed as well.
The available logs do not establish whether the extra latency was CPU, disk,
or another runner resource. Do not hide it behind retries or claim stable
eight-minute builds from one sample.

A local full-file cProfile run of all 224 Harness tests measured 110.61s:
70.94s in migrations, including 179 background database initializations.
Ordinary business tests use explicit temporary database paths, which do not
receive the existing default-home database template. The full migration chain
is repeated even though these assertions concern Harness behavior at head.
Unprofiled local baselines: Harness 63.65s, scheduling 85.39s (489 tests).

The shared cleanup fixtures also import the Web server and SQLite importer into
otherwise unrelated test processes. A small two-test workflow contract profile
spent 0.55s loading/clearing OAuth state and 0.18s loading/clearing SQLite state.
With one interpreter per file, eager cleanup imports repeat across the suite.

## Chosen scope

Reuse the existing migrated, checkpointed template through an opt-in database
factory for business tests. Copy into newly created paths under the current
test's temporary directory only, refuse overwrites, and keep every copy private.
The two slow Harness/scheduling modules seed their explicit fixture paths;
`no_sqlite_template` still leaves migration cases unseeded. Do not patch production
database constructors, disable SQLite durability, or mock migration assertions.

Reset only loaded cache owners, both before and after the test. Resolve owners
again during teardown so a module first imported inside the test is also reset.
Contract tests cover complete template identity, mutation isolation, path safety,
overwrite refusal, migration opt-out, and lazy cleanup lifecycle behavior.

The build-to-install chain in the master baseline finishes around 7m45s;
more unit shards alone cannot remove that bound. Packaged Memory smoke and
installer regressions share an input artifact, not mutable state or outputs.
Run them as two independent matrix jobs, each with its own runner, retaining
the existing required `install-upgrade-regression` check as an always-running,
fail-closed aggregate. Both suites keep the same test commands, artifact source,
and real environment checks. The expected check set grows from 15 to 17.

## Evidence

Local unprofiled comparisons, with every original assertion retained:

| Suite | Tests | Before | After |
| --- | ---: | ---: | ---: |
| Harness failure visibility | 224 | 63.65s | 20.54s |
| Scheduling | 489 | 85.39s | 49.45s |

Real migration suite: 128 passed. Consuming suites run in separate processes:
Web API 114 passed, authorization 68 passed, scheduled dispatch 89 passed plus
14 subtests. Source/workflow package contracts: 14 passed; seven actual-artifact
contracts require CI-built distributions. Ruff 0.4.9 and dependency checks passed.

Record complete CI job timestamps and exact-head review inventory before final
delivery. A runner-specific cause for the observed slowdown remains unproven until
measured directly. Local reductions are not a prediction of CI wall-time savings.

### First CI correction

Run 33959161877 exposed a missing shared prerequisite after the matrix split:
the installer suite's released-generation upgrade test also builds source wheels,
so it needs the Show Runtime manifest just like the packaged Memory suite.
Manifest preparation must run unconditionally on both isolated runners. Keep the
real source-wheel build and the failure, rather than replacing it with an artifact
shortcut or allowing that case to skip. The workflow contract now asserts this
shared preparation explicitly. This is a CI finding; review-head counts are
recorded separately from CI failures.

The consuming upgrade helper was then run locally with the verified release
manifest and built both real `9999.0.0` core and Memory wheels successfully.
Workflow/fixture contracts passed again (16 tests). The unauthenticated manifest
fetch hit a GitHub rate limit locally; the existing validator was used with an
authenticated `gh` transport, preserving release digest and schema validation.

### Measured rebalance

All six unit shards in run 33959161877 passed. Their complete logs contain all
398 discovered files exactly once and no failing file exit codes. Harness took
51s and scheduling 88s, versus 232s and 188s in run 33955817335. Per-file totals
across the six runners were 289s, 318s, 335s, 354s, 364s and 373s. The whole
workflow was **not** green because of the manifest failure above.

The orchestrator extends scope to `scripts/ci_unit_test_timings.json`, refreshing
the complete measured snapshot from those successful unit jobs. The existing
planner replays that sample at 339-340s per shard; this is a balancing estimate,
not an observed CI duration. The algorithm and discovery rules remain unchanged.

Review inventory: head `cfe16d930f` passed via bot comment 5551035775, complete
review/thread collections empty. Zero findings-bearing review heads. The next
push needs its own exact-head review and fully successful CI.

### Clean-run tail diagnosis

Head `305f19859c` passed review (comment 5551088001), all 17 checks, and
all whole-PR thread checks: zero findings-bearing heads and zero threads.
Run 33959745089 took 8m58s, not an improvement over the earlier 8m26s PR
sample. The build completed in 3m22s; the two installation suites took 2m20s
and 2m03s in parallel. Five unit jobs finished in 4m45s-6m23s, but shard zero
took 8m50s. All 398 files ran exactly once without failing file exit codes.

The archive module on the longest shard grew from 27s to 86s between the two
CI samples. A local profile independently found 14.915s in migrations out of
16.738s overall, with 47 migration calls for 41 behavioral cases. The unprofiled
baseline was 11.05s. This does not establish the runner-specific cause of the
CI variance, but it demonstrates removable migration work in another consumer.

The orchestrator extends scope only to `tests/test_vibe_agent_archive.py`,
opting it into the same private database factory. Keep every existing transaction,
write-lock race, immutable-snapshot and rollback assertion intact. Migration tests
remain unseeded. Do not rebalance again from this single runner-skewed sample,
increase runner count, or change production migration behavior.

After the archive fixture change, all 41 cases passed in 2.35s (11.05s baseline).
Fixture/workflow/shard contracts passed again; the existing timing snapshot is
unchanged and the next exact-head CI run will measure the actual overall effect.

### Asynchronous UI test boundary

Head `fae385e6ea` passed bot review (comment 5551151290), with no review threads
or findings-bearing heads. Run 33960329173 completed in 6m18s; all six Python
shards passed in 5m42s-6m08s, as did both installation suites. The workflow still
failed because the modified-Escape Composer test expected an abort callback
before the plain-Escape listener handled the event; the aggregate correctly failed.
Do not report this failed workflow as a delivered six-minute CI result.

The focused nine-test file and the entire local UI suite (283 files, 3766 tests)
pass without changes, so the CI failure has not been reproduced deterministically.
Inspection shows recording starts across resolved promises, while plain Escape
uses a passive effect listener. The test used synchronous `fireEvent`, then DOM
queries, neither of which explicitly flushes the whole asynchronous interaction.
This is consistent with a listener-registration race, not proof of a product bug.

The orchestrator extends scope to `Composer.shortcut.test.tsx` only: wrap the
recording-start interaction in awaited asynchronous `act`, as specified by
React's [test interaction contract](https://react.dev/reference/react/act).
Preserve every finish/abort assertion and real event listener. Do not add sleeps,
retries, relaxed expectations, or production changes. Verify focused and full UI
tests again and obtain new exact-head CI/review evidence before merge.

After the interaction-boundary change: 17 focused shortcut/listener tests and
all 283 UI files / 3766 tests passed. Theme validation, lint baseline, test type
checking and production build passed. In the prior failed run, archive measured
9s (86s before the private fixture); all 398 Python files still ran exactly once.
