# Memory best-effort capture (Phase 1)

> Status: implementation contract
>
> Scope: replace Avibe's durable Memory outbox with a bounded process-local writer.
> Capture loss is intentional; the design optimizes for healthy, unsaturated use
> instead of preserving every item across failures and restarts.
>
> This change is docs-only. "Implementation" below means the follow-up code PR.

## Decision

> **Reliable control plane, best-effort capture data plane.**

Keep EverOS supervision, provider-root confinement, identity, authorization, and
destructive-operation fencing reliable. Treat capture payloads, retries, flush
schedules, per-call provenance, and failure history as disposable.

Most eligible captures should reach EverOS while Avibe and EverOS are healthy and
the writer is not saturated. There is no zero-loss guarantee, delivery SLO,
cross-restart replay, or complete diagnostic history.

## Product contract

### Optimized path

When the Memory runtime and identity store are ready, the request is authorized,
the writer has capacity, and EverOS is reachable:

1. Avibe reserves bounded process-local capacity.
2. It performs bounded local admission work and enqueues the item.
3. One ordered worker calls EverOS.
4. An acknowledgement updates volatile flush state and small summary fields on a
   best-effort basis.

`CaptureAccepted` means only that Avibe accepted volatile work. It does not mean
EverOS stored or processed the content.

### Required guarantees

- Chat, `/new`, archive, configuration replacement, and shutdown never wait for
  queued Memory delivery.
- Attachment preparation, queued items, and in-flight items share an explicit
  process-wide bound.
- A provider operation that may have executed is not replayed.
- Existing authorization, project limits, and path confinement remain fail closed.
- Stable identity and catalog data survive every recognized store upgrade.
- Clear and Factory Reset retain durable authority; malformed or missing markers
  never grant destructive permission.
- Destructive and maintenance operations never race an old provider owner against
  a replaced provider root.
- Logs never contain credentials, provider URLs, captured text, attachment paths,
  or identity digests.

### Accepted loss

An accepted item may be lost when the process exits, the runtime is replaced, the
writer is full, EverOS fails or returns an ambiguous result, attachment handling
fails, a session barrier is missed, or an upgrade discards old outbox work.
Diagnostics may also omit calls, failures, and correlations.

Record bounded content-free counters or logs when practical, but never make that
record a prerequisite for dropping work or accepting later captures. Do not add a
persistent state machine to distinguish every loss window.

Review guardrail: a finding that only exposes one of these loss windows is covered
by this contract. Fix it only if it also breaks the healthy path, a resource bound,
authorization/confinement, stable identity, or control-plane exclusion.

### Out of scope

- EverOS source, API, data format, or identity derivation changes
- exactly-once, at-least-once, durable replay, or complete diagnostics
- durable per-call provenance, anomaly, gap, correlation, retry, pending-flush, or
  attachment-cleanup state
- a new attachment lease design
- automatic Rebuild, Repair, Clear, or Factory Reset
- user-configurable queue, retry, or flush tuning

### `vibe memory remember`

Keep `memory.cli.remembered` (`Memory queued.` in English and existing localized
values) and JSON `ok: true` with `result.status` in `{accepted, duplicate}`. This
copy describes volatile admission, not storage.

## Writer design

### Remove durable delivery state

Delete durable enqueue, claim, lease, retry, settlement, flush FSM,
`manual_required`, tombstone, bundle-ownership, recovery, and drain behavior from:

- `core/memory/store.py`
- `core/memory/coordinator.py` / `SessionFlushCoordinator`
- `core/memory/worker.py` / `MemoryWorker`
- `memory_capture_queue`, `memory_session_flush_state`,
  `memory_flush_settlements`, and `memory_attachment_bundle`
- `MemoryRuntime` recovery loops and `MemoryModule.final_flush` waits

### One bounded path

```text
CaptureAdmission
  validate + authorize + reject duplicates
        |
        v
BestEffortMemoryWriter.try_reserve()
  one of 256 process-local permits
        |
        v
optional attachment pin + short identity transaction
        |
        v
offer_reserved() -> asyncio.Queue -> one ordered worker
        |
        v
EverOSPort.add / flush -> best-effort summary
```

Fixed implementation defaults:

| Bound | Value |
|---|---:|
| writer permits across preparation, queue, and in-flight work | 256 |
| duplicate source-message LRU | 256 |
| total add attempts | 3 |
| pending provider sessions | 256 |
| retained message ids per pending session | 100 |
| flush thresholds | 5 min idle / 30 min age / 100 acknowledgements |

These values are not durability promises or user settings.

### Admission

Admission performs no EverOS I/O and no unbounded wait:

1. Run existing authorization, size, scope, duplicate, and static overflow checks.
2. Atomically reserve one writer permit or return `CaptureSkipped`. The permit
   covers attachment pinning, identity admission, queue residence, provider I/O,
   and terminal cleanup, so concurrent preparation cannot exceed the bound.
3. Pin optional attachments under that permit. Pin failure skips the multimodal
   item and releases the permit after one confined cleanup attempt.
4. In one short admission critical section and bounded SQLite transaction,
   enforce the 16-project limit and allocate
   `max(occurred_at_ms, last_provider_timestamp_ms + 1)`.
5. Convert the reservation to a queue item before leaving that critical section.
   Reserved capacity guarantees this cannot fail because another offer filled the
   queue.

Validation, pin, or identity failure releases the permit. A cancelled generation
returns immediately to its caller, but an underlying pin/provider job keeps its
shared permit until it actually terminates or its sidecar is reaped; replacement
therefore cannot exceed the process-wide bound. No catalog or watermark mutation
occurs without a reservation, and an identity failure creates no recovery item.
Reusing a project is allowed at the 16-project limit; creating a 17th is not.

Every queue entry, including a session barrier, owns one permit until its local
operation is actually terminal. Then one bundle-release attempt returns the permit;
cleanup failure may leak a confined file but never writer capacity.

### Ordered worker and retries

One worker performs provider operations serially and preserves queue order.

An add may use at most three total attempts, and only when the previous failure is
proven to precede provider execution. A possibly submitted timeout, cancellation,
malformed response, transport failure, or unclassified rejection is terminal.
Restart drops the queue and all retry knowledge.

The sole post-call modality fallback is an attachment rejection for which existing
`attachment_add_rejection_proves_no_write()` positively proves no write. Under the
same permit, the worker may make exactly one caption-only call, still within the
three-attempt total. Empty captions and ambiguous or unclassified outcomes drop.

An acknowledged add updates `last_success_at` and volatile flush state. Summary
write failure never creates a retry or blocks later capture.

### Session flush

`PendingFlush` stores only a provider-session reference, raw session reference,
timestamps, count, and at most 100 message ids. It is bounded to 256 provider
sessions and disappears on extraction, runtime replacement, or process exit.

If the tracker is full, keep the successful add but leave its session untracked.
`/new` and archive make one non-blocking attempt to enqueue a session barrier and
return. At queue head, the barrier flushes currently tracked scopes for that raw
session. It may miss an untracked scope, a capture still preparing attachments, or
state lost during a transition.

A flush retries only a failure proven to precede provider execution. Once
submitted, it is consumed regardless of result and never restored to the tracker.

### Runtime transitions

Disable, shutdown, and runtime-affecting configuration replacement stop intake,
cancel the generation, and discard queued and tracked work without draining it.
In-flight calls are not replayed; cancellation is not proof that no write occurred.

Clear and Factory Reset also discard volatile work, but before changing identity or
the provider root they must prove that no old-generation provider RPC can still
execute. Stop and reap the sidecar, or use an equivalent existing bounded
quiescence proof. Cancellation alone is insufficient. If exclusion cannot be
proved, fail the destructive operation closed and leave identity/root untouched.

## Persistent state and migration

After migration, `state/memory/memory.sqlite` contains only:

- `memory_meta`: epoch, Clear fence, scope/root identity, timestamp watermark,
  bounded summary fields, and timestamps
- `memory_projects`: caller principal, project id, and catalog timestamps

Summary fields are not an event log and never gate startup, submission, or later
capture. Stop writing processing-fault/alert/recovery fields. Do not add
`memory_call_provenance`, `memory_processing_anomaly`,
`memory_flush_correlation_guard`, `memory_observation_gap`, or equivalents.

Keep current provider-session derivation:

```text
src--<hmac-sha256(scope_key, "{memory_owner_id}:{project_ref}:{session_id}")>--e{epoch}
```

Keep #1642 ownership: user input uses the 34-character caller principal; agent
provenance uses that caller's derived `-agent` owner; project rows stay keyed by
the caller principal.

Recognize every released v0-v3 shape accepted by the current migrator. Migration:

1. Reconcile readable Factory Reset/Clear intent. An unreadable marker or orphaned
   Clear fence stays blocked until an explicit Clear supplies fresh authority.
2. Recognize the confined SQLite shape without writing.
3. In one transaction, preserve stable identity, project rows, watermark, and
   `last_success_at`; discard delivery, retry, settlement, provenance, failure,
   flush, and attachment-bundle state; install identity-only v4.
4. Commit, then checkpoint, close owned connections, and reopen through SQLite's
   normal WAL lifecycle. Never unlink WAL, SHM, or journal files directly.
5. Best-effort scrub the confined attachment root, then enable text capture.

Any failure before commit rolls back and leaves the prior logical store intact.
Commit is the migration completion point: a later checkpoint, close, or reopen
problem does not roll back v4. A busy checkpoint is not a migration failure; keep
the committed store and retry normal open/startup handling. Unknown nonempty stores
remain untouched and Memory stays unavailable. Downgrade is unsupported.

## Retained surfaces

### Control plane

Keep artifact installation, sidecar ownership, root confinement,
`MemoryOperationLease`, Restart Engine, Rebuild (`cascade rebuild --yes`), Repair
(`cascade sync`), Clear authority, Factory Reset, and root recreation.

Before Rebuild or Repair launches a maintenance child, close writer intake and
cancel its generation. Wait only for the existing bounded provider-RPC quiescence
proof; never drain queued capture. Fail closed if a second provider owner cannot be
excluded. Clear resets identity, catalog, watermark, and summaries only after the
stronger destructive quiescence rule above succeeds.

### Diagnostics

- Provider Call Log records what its independent recorder observes.
- Processing Record shows only data safely derived from EverOS and retained call
  logs. Missing authorization evidence omits the call or marks the source
  unavailable; it never broadens scope.
- Migration, restart, pruning, and ambiguous results may leave history incomplete.
- Diagnostic read/write failure never blocks writer startup or provider calls.

Remove delivery-table joins rather than replacing them with durable gap, guard, or
anomaly ledgers. UI/API responses must distinguish an unavailable source from a
complete empty result.

### Attachments

Use the shared writer permit before pinning. On admission failure or terminal
provider outcome, attempt confined bundle release once, then release the permit
even if file cleanup fails. Log without paths; boot cleanup may reclaim the orphan.

Run existing confined cleanup before enabling attachment capture. If confinement
or cleanup safety cannot be proved, disable attachment capture for that runtime and
continue text capture. Never reconstruct delivery from leftover bundles or touch
original chat attachments.

### Receipts and callers

| Writer outcome | Receipt |
|---|---|
| admitted to volatile writer | `CaptureAccepted` |
| duplicate in local LRU | `CaptureDuplicate` |
| disabled, invalid, busy, over limit, not ready, or full | `CaptureSkipped` with existing closed code |

Automatic capture logs the receipt without content. `/internal/memory/remember`
continues returning `receipt.status`; callers never see delivery-state primitives.

## Delivery plan

Implementation order:

1. Add identity-only v4 migration.
2. Add the bounded writer, one worker, and volatile flush tracker.
3. Switch capture and session lifecycle callers to non-blocking offers/barriers.
4. Simplify diagnostics without weakening authorization.
5. Delete the durable delivery/observer protocol and obsolete tests.
6. Update scenario catalogs, focused tests, and `docs/MEMORY.md` /
   `docs/MEMORY_ZH.md` with the accepted-loss contract.

Prefer one implementation PR. Never ship durable and volatile delivery active at
the same time merely to split the diff.

### Scenario changes

| Scenario | Contract |
|---|---|
| `MEMORY-INDEP-001` | retain non-blocking chat, `/new`, and archive |
| `MEMORY-INDEP-002` | retain permitted stale-capture loss at session transition |
| `MEMORY-INDEP-003` | rewrite shutdown to drop instead of drain |
| `MEMORY-INDEP-008` | rewrite shutdown to avoid capture/flush settlement waits |
| `MEMORY-INDEP-010` | retain lifecycle boundary without a delivery guarantee |
| `MEMORY-INDEP-012` | retain content-free service logging and no IM alert |
| `MEMORY-SEARCH-006` | rewrite final flush as a best-effort bounded barrier |
| `MEMORY-SEARCH-013` | rewrite terminal flush to allow missed captured scopes |
| `MEMORY-IM-ATTACH-009`, `-011` | retain one proven-safe text fallback |

Remove or rewrite scenarios that require replay, `manual_required`, exact
Processing Record history, or shutdown drain.

### Validation

- Every released fixture preserves stable identity/catalog data and removes all
  delivery-shaped rows without provider I/O.
- Injected pre-commit migration failures leave the old logical store unchanged;
  post-commit checkpoint/reopen failures retain committed v4 for normal recovery.
- Unknown schemas, unsafe paths, and ambiguous Clear authority fail closed without
  writes.
- The 256 permits bound attachment preparation plus queued and in-flight work; no
  catalog/watermark mutation occurs without a reservation.
- Healthy admitted captures reach a fake provider in FIFO order; queue and flush
  trackers stay within bounds.
- Offers, barriers, replacement, and shutdown never wait for delivery; transitions
  intentionally discard volatile state.
- Only proven-unsubmitted failures retry. The sole attachment caption fallback is
  single-shot, proven unwritten, and included in the three-attempt maximum.
- Diagnostics failure cannot reject capture, missing evidence never widens access,
  and unavailable sources are reported truthfully.
- Rebuild, Repair, Clear, and Factory Reset retain bounded provider exclusion
  without draining capture.
- Logs, summaries, and receipts remain content-free.

Tests must prove the healthy path, bounds, non-blocking behavior, security, and
intentional drops. They must not assert that every capture survives.

If stronger delivery or diagnostic guarantees become necessary, approve them as a
separate measured capability. Do not silently grow this writer back into an outbox.
