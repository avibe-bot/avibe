# Non-blocking service logging (#2748)

## Change contract

This is a proactive mitigation: the recent current-master soak did not reproduce a
logging-attributed controller-loop stall. Only service logging ownership and its
entry/exit lifecycle change; no controller, database, transport, or call-site
changes are intended.

- Root records go through one QueueHandler; one QueueListener owns the existing
  rotating file and optional stdout destinations, formatter and levels.
- A queue holds at most 8192 pending records (plus the listener's in-flight
  record); it is a record-count bound, not a byte limit. Full queues drop the
  incoming record (including high-severity/final records); accepted records are
  never evicted. Ordinary QueueHandler message/traceback preparation is retained.
- Cumulative `queue_full`, `prepare_error`, `handler_error` and `reentrant`
  counters are reported directly to existing destinations, at most once per
  30 seconds while processing records and once at final drain if totals changed.
  No producer writes to stderr on overload/error. Failing destinations are
  attempted independently and cannot kill the listener. If no destination
  accepts an aggregate diagnostic, the listener writes/flushes a single line of
  at most 1024 ASCII bytes directly to process stderr (`sys.__stderr__`). It
  shares the 30-second summary rate limit plus the final-drain attempt, contains
  only cumulative counters (no record payloads), and never logs or re-enqueues.
  Missing/broken stderr is caught without retries; blocked stderr only blocks
  the listener, not producers. This escape path is best effort, not delivery
  assurance when stderr also fails or shares the unavailable filesystem.
  A diagnostic's own failed delivery is counted, not recursively retried.
  Reporting is activity-driven: isolated preparation errors can wait until a
  subsequent accepted record or shutdown; 30 seconds is not a delivery SLA.
- Nested logging while producing (including a signal interrupting Queue's
  non-reentrant mutex) or inside a destination is suppressed/countable, avoiding
  deadlock and feedback loops. Shutdown signal handling itself still runs; only
  a nested record can be lost under this policy.
- Shutdown closes admission only after controller and desktop/diagnostic
  cleanup. It drains all accepted records, attempts each destination, joins the
  listener and closes owned handlers BEFORE releasing the service instance lock,
  so a successor cannot acquire ownership while this logger still rotates files.
  Lock release remains in a `finally` and retains the existing lock-safety gate.
  Setup/shutdown are repeatable; partially constructed setups leave no listener
  or file behind.
  New destinations open only after the prior drain/rotation finishes. Previous
  root handlers and level are detached while the runtime owns root, then restored
  on stop; they are not closed by this runtime. A closed admission handler stays
  on root throughout drain to prevent implicit synchronous stderr configuration.
  `main()` explicitly drains even if cleanup raises; an atexit hook is a fallback
  for other callers. Sink close errors do not prevent the other sinks closing.
  The service deliberately owns root configuration; third-party root handlers
  are inactive during ownership, and concurrent third-party reconfiguration is
  not supported. Lock-release debug diagnostics and late daemon records after
  drain use restored pre-service logging rather than this service file.
- Drain is not a disk-durability promise: failed destinations cannot guarantee
  delivery. Lossless draining also cannot bound exit time if a destination never
  returns (including stderr fallback). The service retains its lock during that
  wait. The existing POSIX managed stop gives the WHOLE shutdown five seconds
  before SIGKILL; cleanup and logging share that grace, and this change does not
  extend it. Windows managed termination also does not promise Python drain.
  Forced process termination can lose queued/in-flight records and skips atexit.
  Drain is conditional on destination progress and absence of forced termination,
  NOT a bounded-shutdown promise. Normal logging still performs record/message
  preparation and short in-process lock operations.
- No deployment, service restart, Incus update, or merge is authorized here.

## Validation plan

Existing tests only exercise handlers synchronously and the surrounding service
lifecycle; they do not exercise asynchronous acceptance or drain. Extend that
owner boundary with deterministic blocked destinations: the producer must finish
before release, accepted records must arrive after release, overflow must be
counted without recursive diagnostics, and shutdown must wait for actual final
record delivery. Exercise real file rotation/stdout suppression, handler errors,
setup failure/repetition, early startup failure and final service cleanup logging.
Run focused lifecycle tests, changed-file Ruff and relevant repository checks;
leave full platform/suite coverage to CI.

## Progress

- [x] Inspect setup, shutdown and existing logging tests on current origin/master.
- [x] Implement runtime and service lifecycle ownership.
- [x] Prove slow-I/O, overflow, drain, failure and compatibility contracts.
- [x] Inspect complete working-tree diff, including staged/unstaged changes.
- Exact-head review/CI delivery evidence will be recorded in the PR.

## Local evidence

- Combined logging, lifecycle, config compatibility, controller shutdown,
  watchdog and service-lock selection: 199 passed, 3 platform skips and 4
  subtests passed on Python 3.12 after the final ordering/fallback revision.
- File-only write/rotation failures exercise successful, missing, write-failing
  and flush-failing stderr; bounded diagnostics, rate limiting and blocked stderr
  producer independence are verified. Replaying the pre-fallback reporter makes
  the regression fail because no stderr notice arrives.
- The slow-stream regression was replayed with `setup_logging` extracted from
  origin/master: it failed specifically at `controller waited for stream.write()`;
  the same test passes with the queue runtime.
- Changed-file Ruff and mypy (production files, imports skipped) pass.
- No live service/Incus operations were performed. Full cross-platform coverage
  remains with CI; the local environment is hermetic and owns its streams/files.
