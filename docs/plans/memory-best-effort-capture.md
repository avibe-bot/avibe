# Memory best-effort capture (Phase 1)

> Status: implementation contract
>
> Scope: replace Avibe's durable Memory outbox with a bounded process-local writer.
> Capture loss is intentional; the design optimizes for healthy, unsaturated use
> instead of preserving every item across failures and restarts.
>
> This PR implements the contract below in the same change. The implementation and
> focused scenario rewrites are part of this PR; the contract is not a planning-only
> follow-up.

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

Keep `memory.cli.remembered` as an explicit best-effort acknowledgement and JSON
`ok: true` with `result.status` in `{accepted, duplicate}`. This copy describes
volatile admission, not storage.

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
| provider attempts per queue item | 3 |
| pending provider sessions | 256 |
| retained message ids per pending session | 100 |
| flush thresholds | 5 min idle / 30 min age / 100 acknowledgements |

These values are not durability promises or user settings.

### Admission

Admission performs no EverOS I/O and no unbounded wait:

1. Run existing authorization, size, scope, and request overflow checks.
2. Under one process-local lock, reserve a permit and claim the source digest as a
   non-evictable pending entry; existing digests return `CaptureDuplicate`.
3. Pin optional attachments under that permit. Pin failure must prove confined
   cleanup or latch attachment intake off before releasing the permit; admit a
   non-empty caption as text-only, while attachment-only input drops.
4. In one short admission critical section and bounded SQLite transaction, compute
   candidate catalog and timestamp state. Reject a 17th project or a value above
   `MAX_PROVIDER_TIMESTAMP_MS` before mutating either durable field.
5. Remove a failed pending entry. Otherwise convert the reservation to a queue item
   and mark it admitted/evictable in the 256-entry LRU under the same lock.

Every reservation stays generation-visible through pinning, its bounded transaction,
and terminal queue conversion or failure. A cancelled generation returns immediately,
but old work keeps its permit until joined or reaped; no late catalog or watermark
commit may cross a transition. Rejected candidates leave state unchanged and create
no recovery item; existing projects remain usable at the limit.

Every queue entry, including a session barrier, owns one permit until its local
operation is terminal; attachment cleanup follows the bounded rule below.

### Ordered worker and retries

One worker preserves ready-queue order. Concurrent attachment preparation may
reorder caller arrival; arrival-order FIFO is not a contract.

Each add or flush has at most three attempts, only after proven pre-execution failure.
A possibly submitted timeout/cancel/transport loss is not retried: the worker starts
the existing bounded owned-sidecar stop/reap and holds its permit until termination
is proved; failure leaves Memory unavailable. Malformed/unclassified responses end.

The sole post-call modality fallback is an attachment rejection for which existing
`attachment_add_rejection_proves_no_write()` positively proves no write. Under the
same permit, the worker may make exactly one caption-only call, still within the
three-attempt total. Empty captions and ambiguous or unclassified outcomes drop.

An acknowledged add updates `last_success_at` and volatile flush state. Summary
write failure never creates a retry or blocks later capture.

### Session flush

`PendingFlush` stores only refs, timestamps, count, up to 100 message ids, and one
scheduled bit. At most 256 sessions are tracked; state disappears on extraction,
runtime replacement, or process exit.

One process-local scheduler drives idle/age/count thresholds without per-session
tasks. An offer failure defers five minutes; success marks it scheduled but
barrier-visible until dequeue. An earlier barrier may extract it; the offer then no-ops.

If the tracker is full, keep the successful add but leave its session untracked.
`/new` and archive make one non-blocking attempt to enqueue a session barrier and
return. At queue head, the barrier flushes currently tracked scopes for that raw
session. It may miss an untracked scope, a capture still preparing attachments, or
state lost during a transition.

A flush uses the same three-attempt cap. Once submitted, it is consumed regardless
of result and never restored to the tracker.

### Runtime transitions

Shutdown stops intake, cancels the generation, and drops queued/tracked work without
draining; process teardown reaps owned work.

Disable or any transition that changes provider authority or its root -- runtime
configuration replacement, Restart Engine, Rebuild, Repair, Clear, or Factory Reset
-- drops volatile work; one bounded barrier stops/reaps old RPCs and joins every
old-generation reservation through pinning, transaction, and terminal conversion
before authority publication, provider replacement, maintenance, or deletion.
Cancellation alone is insufficient; failed exclusion keeps old authority/root and fails closed.

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
3. In one transaction, preserve identity, catalog, timestamp watermark, and
   `last_success_at` exactly from each released shape, deriving v0-v2 catalog rows
   from queue data; no helper commits independently. Discard delivery state; install v4.
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

All authority-changing operations use the barrier; none drains or adds delivery
state. Clear retains four confined deletion surfaces: identity/catalog/watermark/summary state,
provider root, call log, and attachments; it completes only after all four succeed.

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

Use the shared permit before pinning and attempt confined bundle release once at a
terminal outcome. If release fails, atomically latch attachment intake off for that
runtime before returning the permit; only already-reserved work may add a bounded
number of orphans. Log without paths.

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
| `MEMORY-INDEP-001`, `-002`, `-012` | retain non-blocking lifecycle, accepted loss, and content-free logs |
| `MEMORY-INDEP-003`, `-007`, `-008` | rewrite shutdown to drop; archive offers one barrier then releases authorization without settlement waits |
| `MEMORY-INDEP-010` | rewrite lifecycle snapshots around a non-blocking barrier offer; a capture still preparing or belonging to a stale generation may be missed or invalidated |
| `MEMORY-SEARCH-005`, `-006`, `-012`, `-013`, `-014`, `-017` | rewrite v4 migration/diagnostics; remove reopen recovery; bound flush |
| `MEMORY-SEARCH-016`, `MEMORY-IM-ATTACH-001`, `-003`, `-010` | retain healthy/fallback semantics; replace drain/final flush with test-only worker/barrier sync |
| `MEMORY-REBUILD-202`, `MEMORY-REPAIR-006` | rewrite claim/sidecar assertions around the transition barrier |
| `MEMORY-CLEAR-201`, `MEMORY-FACTORY-003`, `-004`, `-201` | barrier covers post-pin admission; Clear resets stable identity and drops volatile work without replay |
| `MEMORY-IM-ATTACH-004`, `-009`, `-011`, `-013` | retain cleanup/fallback; bound cleanup failure |

Remove only scenarios whose product contract requires replay, exact history, or drain.

### Validation

- Every fixture preserves identity/catalog, exact watermark, and exact `last_success_at`;
  v0-v2 catalog derives from queue rows and delivery rows vanish without provider I/O.
- Pre-commit failures leave the old store unchanged with no helper commit;
  post-commit checkpoint/reopen failures retain v4 for normal recovery.
- Unknown schemas, unsafe paths, and ambiguous Clear authority fail closed without
  writes.
- The 256 permits bound preparation plus queued/in-flight work; rejected project or
  timestamp candidates leave catalog and watermark unchanged.
- Ready items preserve queue order; queued flushes remain barrier-visible, concurrent
  preparation may reorder arrivals, and queue/flush trackers stay within bounds.
- Offers, barriers, replacement, and shutdown never wait for delivery; transitions
  intentionally discard volatile state.
- Adds/flushes stop after three attempts; ambiguous calls initiate bounded reaping;
  pin failures and proven-unwritten rejection preserve non-empty captions.
- Diagnostics failure cannot reject capture, missing evidence never widens access,
  and unavailable sources are reported truthfully.
- Authority changes quiesce old RPCs and every admission through queue conversion;
  no drain occurs, and cleanup failure disables further pinning.
- Logs, summaries, and receipts remain content-free.

Tests must prove the healthy path, bounds, non-blocking behavior, security, and
intentional drops. They must not assert that every capture survives.

If stronger delivery or diagnostic guarantees become necessary, approve them as a
separate measured capability. Do not silently grow this writer back into an outbox.
