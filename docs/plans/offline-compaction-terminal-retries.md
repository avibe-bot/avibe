# Offline compaction and terminal persistence retries

## Change contract

Full SQLite compaction is an explicit offline operation. `vibe data retention
--compact` must refuse an active service or UI without stopping either, and hold
the existing lifecycle/start exclusion for the complete rewrite. Ordinary
batched retention without compaction remains online. No incremental-vacuum
conversion or retention policy change is included.

Retries belong at pure, complete transaction boundaries: command definition/result
stamps (including their atomic escalation outbox), claimed Run terminal settlement,
and message mirror scope+message writes. Reuse `retry_sqlite_write`'s bounded BUSY
policy and preserve cancellation and ownership guards. No retry wraps command
spawn, transport delivery, or post-commit publication. Blocking writes/retry waits
run off the controller loop; publication stays with its existing owner.

A completed command's actual exit/stdout/stderr outcome must not disappear
because its definition projection failed. Exhaustion is logged and handed back
to the existing reconciliation owners, with known current-process outcomes kept
for a persistence-only retry. This is not a new orphan-run classification or a
promise to reconstruct uncommitted output after a hard process crash. Already
started commands are never re-enqueued as persistence recovery.

## Scope and validation

Base: `30fcb3bb6` after the resident-definition contract (#3341). No schema change
is intended. The work preserves that lane's exact consumed-generation and
retired-definition ownership rules.

Tests cover a real writer hold beyond SQLite's five-second busy timeout, bounded
exhaustion, committed-state guard rechecks, no duplicate external work or
publication, off-loop liveness, and command outcome survival across a failed
projection. Offline-compaction tests exercise service/UI refusal and a concurrent
startup attempt while the rewrite still holds its exclusion. All fixtures use
isolated state; no live service stop, restart, or deployment.

Excluded: incremental vacuum, history retention/trimming, reporter-specific
orphan settlement, a new global writer system or disk recovery journal, and
Activity completion/acknowledgement retries (see the deferred section below).

## Implementation boundaries

- Offline admission retains the raw service lock and the UI-start gate from
  before SQLite/bootstrap work through the final checkpoint, lease cleanup,
  and engine disposal. UI direct entry, ordinary launch, and reload participate;
  no service identity is claimed and no process is stopped. Direct UI entry
  refuses a different live or unverifiable recorded UI owner before publishing
  its PID; ordinary launch/reload can retain their already-published child PID.
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
- Message mirrors freeze original inputs, retry the complete scope/message/media
  transaction, then publish once on the caller loop. Show backfill uses its
  existing ordered worker, without making synchronous Turn lifecycle hooks async.
  See `message-mirror-transaction-retries.md` for that boundary's detailed contract.

## Known by design

1. Retry budgets are per pure transaction, not per command lifecycle. There is
   no global timeout increase or global writer queue.
2. Retained current-process command outcomes are not a disk journal. A process
   exit before their terminal write commits still loses those uncommitted facts;
   existing started-Run recovery remains authoritative. No orphan command replay
   or new orphan settlement policy is introduced.
3. Activity completion, acknowledgement, deferred Activity Run settlement, and
   their recovery lane retain their existing implementation. This change does
   not add Activity pending intents, async lifecycle APIs, or new retry waits.
4. Legacy inbound mirroring/backfill remains observable best-effort on exhausted
   writes. There is no new replay journal. Existing dispatcher and Vault recovery
   owners continue to consume the returned failure result.
5. Non-BUSY failures are not classified as transient writer contention. A command
   projection diagnostic is distinct from the subprocess's exit status; a failed
   terminal transaction keeps its existing visible-error behavior.
6. No migration, history deletion policy, incremental vacuum, service operation,
   or deployment is part of this change.

## Deferred: Activity completion retry

The proposed worker/CAS completion and terminal-acknowledgement slice was removed.
Making full VACUUM offline eliminates the motivating long writer, while existing
bounded retries already address ordinary contention. The remaining demonstrated
benefit did not justify expanding the lifecycle on every Claude turn. No follow-up
implementation is included; reconsider only with fresh evidence of residual loss.

The investigation identified obligations for any future design:
- Preserve activation/incarnation ownership through worker commit, compensation,
  cancellation, and the entire loop-owned notification batch.
- Resume the exact committed terminal-ack restore/cleanup stage across BUSY;
  distinguish CAS ownership loss, and finish cleanup even if a snapshot disappears.
- Keep frozen terminal facts separate from authoritative classification metadata,
  including classification newer in memory than disk. Foreground snapshots
  classified before retry publication must retire without settling their Run.
- Preserve recovered-output registration, grace, and scan-cursor wakeups without
  duplicating native events, delivery, or publication.

## Validation evidence

Hermetic SQLite probes hold an independent writer until the target connection
actually reports BUSY after its ordinary 5000ms handler budget, then allow the
retry to commit. Heartbeats and raw publication counts establish off-loop waits
and single publication; timer-only elapsed assertions are not the retry oracle.
Other retained tests cover exhaustion, whole-attempt rollback, exact
consumed-generation and terminal-CAS ownership, cancellation, retained outbox
stages, post-commit reload failure, and post-terminal follow-up ordering. Non-BUSY
failures release current ownership without changing a completed command's result.

Pre-fix proofs fail for the intended defects: compaction mutates eligible rows
while the service lock is held; a command stamp exception discards its completed
result; mirror owners omit transaction retry/rebuild; and a duplicate direct UI
entry replaces a live owner's PID record. Parent UI handover and dead-owner
replacement remain admitted. After removing the Activity slice, 851 scheduler,
failure-visibility, running-Agent and SQLite-retry tests passed, plus 657
Activity/Claude/dispatcher/mirror/Show/runtime consumers and 35 subtests.
Changed-file Ruff and `git diff --check` passed. Exact-base comparisons confirm
the Activity registry/store/service and its lifecycle entry points were restored.
PR CI and exact-head Codex review remain mandatory delivery gates.
