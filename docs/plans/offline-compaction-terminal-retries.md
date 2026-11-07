# Offline compaction and terminal persistence retries

## Change contract

Full SQLite compaction is an explicit offline operation. `vibe data retention
--compact` must refuse an active service or UI without stopping either, and hold
the existing lifecycle/start exclusion for the complete rewrite. Ordinary
batched retention without compaction remains online. No incremental-vacuum
conversion or retention policy change is included.

Terminal persistence retries belong at pure, complete transaction boundaries:
command definition/result stamps (including their atomic escalation outbox),
Run terminal settlement, Session Activity completion, and message mirror
scope+message writes. Reuse `retry_sqlite_write`'s bounded BUSY policy and preserve
all cancellation, teardown, and generation guards. No retry wraps command spawn,
transport delivery, or post-commit publication. Blocking writes/retry waits run
off the controller loop; loop-owned registry transitions stay on that loop.

A completed command's actual exit/stdout/stderr outcome must not disappear
because its definition projection failed. Exhaustion is logged and handed back
to the existing reconciliation owners, with known current-process outcomes kept
for a persistence-only retry. This is not a new orphan-run classification or a
promise to reconstruct uncommitted output after a hard process crash. Already
started commands are never re-enqueued as persistence recovery.

## Scope and validation

Base: `30fcb3bb6` after the resident-definition contract (#3341). No schema change
is intended. The work must preserve that lane's exact consumed-generation and
retired-definition ownership rules.

Tests will cover a real writer hold beyond SQLite's five-second busy timeout,
bounded exhaustion, committed-state guard rechecks, no duplicate external work
or publication, off-loop liveness, and command outcome survival across a failed
projection. Offline-compaction tests will exercise service/UI refusal and a
concurrent startup attempt while the rewrite still holds its exclusion. All
fixtures use isolated state; no live service stop, restart, or deployment.

Excluded: incremental vacuum, history retention/trimming, reporter-specific
orphan settlement, and a new global writer system or disk recovery journal.

## Implementation boundaries

- Offline admission retains the raw service lock and the UI-start gate from
  before SQLite/bootstrap work through the final checkpoint, lease cleanup,
  and engine disposal. UI direct entry, ordinary launch, and reload participate;
  no service identity is claimed and no process is stopped.
- Worker-owned command stamps opt into bounded transaction retry. Creation/edit
  and legacy synchronous lifecycle callers do not acquire new sleeping retries.
  Atomic escalation outbox attempts rebuild mutable values and publish only
  committed snapshots outside the retry loop.
- The current execution retains the exact completed command facts and, when
  necessary, its original atomic stamp/outbox intent. The REQUESTS lane retries
  those persistence stages, not execution. Each committed stage is removed from
  the intent before the next await, so a later Run write failure cannot replay
  an escalation or its publication. Delivery reconciliation, callbacks, and queue
  recovery run only after the terminal write has landed and before releasing the
  original session exclusion; follow-up errors do not replay persistence.
- Only BUSY exhaustion retains claim/definition-lock ownership for retry.
  Non-BUSY projection errors remain visible but preserve the command's actual
  result and release ownership rather than indefinitely blocking later fires.
  A mirror reload error after an outbox commit invalidates the local mirror; it
  is not reported as transaction failure and cannot authorize replay.
- Live Activity completion and retained-terminal acknowledgement use frozen,
  conditional store writes on workers. Activation/incarnation checks, memory
  transitions, notifications, and wakeups stay on the owning loop. The existing
  ACTIVITY_OUTPUTS lane scans failed completion intents; when reconciliation first
  publishes an output receipt, it registers that receipt as recovered under the
  same lock and respects the existing output grace. New pending work rewinds the
  lane cursor. The AgentService idle sweep retains failed Run-notification/ack
  operations. Deferred settlement rechecks durable sibling Activity blockers
  under its writer reservation.
- Message mirrors freeze original inputs, retry the complete scope/message/media
  transaction, then publish once on the caller loop. Show backfill uses its
  existing ordered worker, without making synchronous Turn lifecycle hooks async.
  See `message-mirror-transaction-retries.md` for that boundary's detailed contract.

## Known by design

1. Retry budgets are per pure transaction, not per command or Activity lifecycle.
   There is no global timeout increase or global writer queue.
2. Retained current-process command outcomes are not a disk journal. A process
   exit before their terminal write commits still loses those uncommitted facts;
   existing started-Run recovery remains authoritative. No orphan command replay
   or new orphan settlement policy is introduced.
3. Existing synchronous Activity start/progress, teardown, output-receipt binding,
   and acknowledgement APIs are not globally converted to async or given new
   retry waits. The new live completion path has an explicit worker/loop split.
4. Legacy inbound mirroring/backfill remains observable best-effort on exhausted
   writes. There is no new replay journal. Existing dispatcher and Vault recovery
   owners continue to consume the returned failure result.
5. Non-BUSY failures are not classified as transient writer contention. A command
   projection diagnostic is distinct from the subprocess's exit status; a failed
   terminal transaction keeps its existing visible-error behavior.
6. No migration, history deletion policy, incremental vacuum, service operation,
   or deployment is part of this change.

## Validation evidence

Hermetic SQLite probes hold an independent writer until the target connection
actually reports BUSY after its ordinary 5000ms handler budget, then allow the
retry to commit. Heartbeats and raw publication counts establish off-loop waits
and single publication; timer-only elapsed assertions are not the retry oracle.
Other tests cover exhaustion, whole-attempt rollback, exact consumed-generation
and terminal-CAS ownership, cancellation, new Activity siblings across an await,
retained outbox stages, post-commit reload failure, and post-terminal follow-up
ordering. Non-BUSY failures release current ownership without changing a completed
command's actual result.

Pre-fix proofs fail for the intended defects: compaction mutates eligible rows
while the service lock is held; a command stamp exception discards its completed
result; and mirror owners omit transaction retry/rebuild. Activity's pre-fix
native completion abandons a contended event instead of handing off its frozen
intent. The integrated scheduler/SQLite-retry/offline/mirror/Show/AgentService
suite passed 714 tests; the integrated Activity/registry/Claude/activation/dispatcher
suite passed 436 tests and 35 subtests (Python 3.12.16, SQLite 3.53.1).
Recovery coverage includes a failed native completion delivered exactly once
without another native event, both with immediate pickup and with output grace.
Changed-file Ruff and `git diff --check` passed. All checks use isolated state;
PR CI and exact-head Codex review remain delivery gates.
