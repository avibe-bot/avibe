# CI Wait Attribution

## Outcome and Order

The owner approved diagnosing long-tail waits, then removing proven repeated
test preparation, then reviewing the remaining installation chain. Start from
master `3619b19d9`. This first PR is observational, not completion of that order.

Seven first-attempt green runs each covered all 405 selected files. Workflow wall
time ranged from 6m04s to 9m00s. On the identical final PR/merge source trees,
config-save tests took 18.72s/46.35s while process CPU fell from 16.51s to 11.20s.
Show and media tests on the same shard also grew in elapsed time. Existing CPU,
context-switch and byte counters cannot attribute that gap to scheduler delay,
disk latency, database locks, child processes, or application waits.

## Scope and Measurement Contract

Only the existing per-file metrics helper, its consuming pipeline tests and this
plan change. Retain the original selection, one interpreter per file, six shards,
300-second visible watchdog, 20-minute job bound, all 17 checks and their
fail-closed aggregates. No new dependency, sampling thread, runtime hook,
privileged command, kernel setting or application change is needed.

Take two bounded, read-only snapshots: at metrics initialization (after the
launcher imports pytest) and just after pytest returns, before interpreter
shutdown. Emit their interval separately from the existing launcher wall and
process-lifetime counters. The existing shell Finished line remains authoritative.

- Thread CPU and Linux schedstat deltas belong only to the launcher thread,
  not all application threads or children. A changed thread identity invalidates
  thread deltas. Runqueue time means runnable but waiting to receive a CPU.
- Require schedstats enabled at both endpoints before exposing scheduler deltas.
  Disabled, unavailable, malformed or decreasing counters produce null, not a
  fabricated zero. No attempt is made to enable kernel accounting.
- CPU, I/O and memory PSI deltas describe the whole host during that interval,
  not this process. They are contextual evidence, not per-test I/O/lock latency.
  System-wide CPU full pressure is undefined and deliberately omitted.
- Do not subtract overlapping or differently scoped counters to label a residual
  as disk time. Neither absent counters nor host pressure alone proves a cause.
  These observations cannot retroactively explain prior runs.

The kernel documents [schedstat counters](https://docs.kernel.org/scheduler/sched-stats.html)
and [PSI scope and units](https://docs.kernel.org/accounting/psi.html).

## Verification and Delivery

Contracts cover counter parsing, availability, disabled accounting, changes at
the interval boundaries, reset counters, thread identity, nanosecond/microsecond
units, and host/thread separation. Existing real subprocess contracts continue
to verify collection, execution, teardown, child CPU and failed process exits.

Require a new head-bound bot pass, complete review/thread inventory, every
exact-head lint run and all 17 checks before guarded integration. Inspect all
405 file exits and metrics, plus actual distribution/installation suites, then
the exact merged-source CI. Record the observed wait evidence before choosing
the next bounded preparation optimization. Do not rebalance from variance or
replace real migration, locking, legacy-import or package-install coverage.

Local validation: 60 pipeline/metrics cases, 6 shard contracts and 12 private
database fixture contracts passed in separate processes. Ruff 0.4.9, dependency
consistency (84 packages) and whitespace checks passed. The Linux readers have
synthetic parser/interval coverage on macOS; actual hosted counter availability
and full-suite noninterference still require this head's CI. Review inventory
starts at zero findings-bearing heads. No previous PASS applies to this PR.
