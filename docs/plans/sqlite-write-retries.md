# Bounded retries for worker-thread SQLite writes

Status: implemented by `fix/sqlite-turn-write-retries`.

## Background

Concurrent processes can keep SQLite's writer busy beyond the 5 s
`busy_timeout` (`storage/db.py`). Canonical transcript commits
(`SQLiteTranscriptStore._commit`) had that one wait and no retry, so exhausted
contention could abort an Agent Turn. Run activity and the Delivery outcome
projection can fail the same way, but their callers already contain the failure.

Startup writer removal is a separate change (#3280); this one does not depend
on it. Moving loop writers off the loop is #2706, and message persistence off the
loop is #2725. The parked global-writer design (#2726) is not revived.

## Contract

`storage.db.retry_sqlite_write(operation)` runs a complete write transaction at
most three times, sleeping 50 ms and then 100 ms between attempts.

- **What is retried.** Only `sqlite3.OperationalError`, bare or wrapped in
  SQLAlchemy's `OperationalError`, whose primary SQLite code is `SQLITE_BUSY`
  (`BUSY_SNAPSHOT` included). Python 3.10 has no `sqlite_errorcode`, so there
  the exact message `database is locked` stands for it. `SQLITE_LOCKED`
  ("database table is locked"), other `OperationalError`s, and every other
  exception propagate on the first attempt.
- **What an operation must be.** It opens its own transaction, rolls it back
  and closes it before raising (`engine.begin()` does), and repeats every read
  it decides on. It has no external effect: no model request, tool call, network
  request, delivery, or publication that can fail after commit and be replayed.
  A publication that follows the commit must contain its own failures, as
  `run_update_event_transaction` does.
- **Where it runs.** Worker threads only: it sleeps synchronously.
- **Bound.** Each attempt can wait the full `busy_timeout`, so an operation can
  wait about 3 × 5 s + 0.15 s ≈ 15.15 s for SQLite's writer before its last
  error propagates.

## Callers

| Path | Owner | After exhaustion |
| --- | --- | --- |
| Canonical transcript writes, `SQLiteTranscriptStore._commit` | Vibey Turn | Raises, as before: no uncommitted transcript entry is reported as written. |
| Run activity, `SQLiteBackgroundTaskStore.record_run_activity` | Dispatcher flush | Raises to the dispatcher, which logs and continues. |
| Delivery outcome projections in `_submit_scheduled_turn` (`_record_outcome`, `_record_deferred_outcome`) | Internal server | Logged; the submission answers as before. |

Invariants kept:

- Transcript writes stay under the per-Session `asyncio.Lock`, in order.
  Each attempt allocates `context_seq` and re-runs its dedupe reads in a fresh
  transaction, and a row is exposed only after its commit. Rendering and
  canonicalization happen once, before the transaction. A cancelled write still
  settles, retries included, before the cancellation escapes.
- Run activity keeps the one observation time taken before the first attempt,
  and every attempt re-reads status and the monotonic guards. A retry never
  becomes a synthetic heartbeat, and lifecycle state is never overwritten.
- Run and Delivery ownership are unchanged. The deferred projection reserves the
  writer before it reads the Delivery state it records, so each attempt waits
  for the writer and records the state current at its commit.
- Only the projection transactions are retried. Admission, `manager.deliver`,
  native dispatch, and publication are not.

Not in scope: an outbox or durable recovery for projections that exhaust their
attempts, retention or history deletion, and changes to the busy timeout.

## Validation

Real SQLite contention on hermetic databases: all nine new path-regression
cases fail on the pre-fix base because the write is not retried. The retry
classifier's parameterized cases additionally check error selection and budget.
The four focused test files pass together (238 tests), and changed-file Ruff
passes. On macOS, the combined test process used a shell-local file-descriptor
limit of 4096; the default 256 was exhausted by the existing multi-file suite.
No production limit or configuration changed.

- `tests/test_sqlite_write_retry.py`: which errors are retried, the delays, and
  exhaustion.
- `tests/test_agent_transcript.py`: a commit that fails after its inserts is
  retried whole, and a queued write on the same Session commits after it in
  order; exhaustion leaves no rows; a cancellation during a retry escapes only
  after the commit.
- `tests/test_harness_live_status.py`: Run activity retries with its original
  observation time, and exhaustion leaves the Run unchanged.
- `tests/test_internal_server.py`: the normal and deferred outcome projections
  retry after a contended first attempt, and exhaustion keeps the submission's
  answer and the Run's ownership.
