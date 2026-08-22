# Memory best-effort capture (Phase 1)

> Status: simplified implementation contract
>
> Scope: replace Avibe's durable Memory outbox with a process-local writer.
> The implementation deliberately accepts capture loss. It optimizes for the
> healthy, unsaturated common case instead of preserving every capture across
> failures and restarts.
>
> The change that lands this file is docs-only. "This PR" / "implementation"
> below means the follow-up product-code PR.

Base: `origin/dev` after #1635. Rebase onto current `origin/dev` before coding.

This revision replaces the over-specified contract originally merged in #1635.
In particular, it does not rebuild the removed outbox as a durable observer
protocol. Memory diagnostics are allowed to be incomplete when capture delivery
is incomplete.

## Decision

> **Reliable control plane, best-effort capture data plane.**

Avibe keeps the EverOS process, provider root, identity, authorization, and
destructive-operation fencing reliable. Capture payloads, retries, flush
schedules, per-call provenance, and failure history are disposable.

The product accepts data loss when any of the following would otherwise require
a second durable delivery or observation system:

- the Avibe process exits or its Memory runtime is replaced
- the bounded process-local queue is full
- EverOS is unavailable, slow, or returns an ambiguous result
- an attachment cannot be pinned or released
- a session-boundary flush is missed
- diagnostics cannot correlate or retain a provider call
- an upgrade discards old outbox work

This is intentional, not a temporary correctness gap. The target is that most
captures succeed while Avibe and EverOS are healthy and the queue is not
saturated. There is no zero-loss guarantee, delivery SLO, cross-restart replay,
or exact diagnostic completeness contract in this phase.

## Background

Avibe currently persists every eligible capture in
`state/memory/memory.sqlite` and delivers it through claims, leases, generation
fences, flush settlements, tombstones, and `manual_required`. That protocol is
a second reliability system around EverOS.

Ordinary chat does not wait for Memory, and processing failures no longer notify
IM administrators. EverOS already keeps accepted markdown and
`unprocessed_buffer` in its provider root. Replaying Avibe's queue after an
ambiguous add can also duplicate content.

The simpler design removes Avibe's durable delivery guarantee while preserving
the identity required to reach already-released provider roots.

## Goals

1. Automatic capture and `vibe memory remember` never wait on EverOS.
2. Healthy, unsaturated captures are attempted in order and normally reach
   EverOS.
3. Capture payloads, retry state, and flush schedules never survive an Avibe
   process restart or Memory runtime replacement.
4. Ambiguous EverOS add or flush outcomes are never retried.
5. `/new`, archive, disable, configuration replacement, Clear, Reset, and
   shutdown never wait for Memory delivery to drain.
6. Every released Memory SQLite shape loads. Stable identity, the provider root,
   catalog, and timestamp watermark survive; old delivery rows are discarded.
7. Sidecar supervision, Rebuild, Repair, Clear, Factory Reset, Processing
   Record, Provider Call Log, and attachment capture remain available with the
   degraded diagnostic guarantees stated below.
8. The implementation deletes substantially more delivery machinery than it
   adds.

## Non-goals

- No EverOS source, API, or data-format change.
- No exactly-once, at-least-once, or durable replay guarantee.
- No durable per-call provenance, anomaly, correlation guard, observation gap,
  pending-flush, retry, or attachment-cleanup ledger.
- No promise that Processing Record reproduces every pre-upgrade call or
  failure.
- No ephemeral attachment-lease redesign.
- No Repair/Rebuild rename, Clear/Factory-Reset collapse, Settings Test, or
  `cascade sync` removal.
- No principal, project, epoch, or provider-session derivation change.
- No automatic rebuild, repair, clear, or reset.
- No user-configurable queue, retry, or flush tuning in this phase.

## Product contract

### Common-case behavior

Inside the normal operating envelope:

- the Memory runtime is enabled and ready
- the identity store is writable
- the process-local admission queue has capacity
- the EverOS sidecar is reachable
- the request passed existing authorization, size, and scope checks

`capture()` accepts the item with bounded local work, one ordered worker calls
EverOS, and an acknowledged result updates the process-local flush tracker. This
path is covered by automated tests and is the behavior the product optimizes.

`CaptureAccepted` means only that Avibe accepted process-local work. It never
means EverOS stored or processed the item.

### Hard guarantees

- Chat, `/new`, archive, and shutdown do not await EverOS capture delivery.
- The capture queue and every process-local tracker have explicit bounds.
- A submitted ambiguous provider operation is not replayed.
- Existing authorization and provider-root confinement never fail open.
- `scope_key`, `provider_root_id`, `epoch`, named-project rows,
  `last_provider_timestamp_ms`, and `last_success_at` survive a recognized
  upgrade unchanged.
- Clear and Factory Reset retain their existing durable authority and cannot be
  inferred from missing or malformed markers.
- Credentials, provider URLs, captured text, attachment paths, and identity
  digests stay out of logs.

### Accepted loss

An accepted capture may be lost before, during, or after its provider call. In
particular, loss is accepted at process/runtime transitions, queue saturation,
provider failures, attachment failures, and flush-boundary failures. The writer
records bounded content-free counters or logs when practical, but recording a
loss must never become a prerequisite for dropping the item or continuing later
capture.

The implementation must not add a persistent state machine to distinguish every
possible loss window. When it cannot prove a provider result, it drops the
operation and continues.

Review rule: a finding whose only consequence is that a crash, restart,
saturation event, provider ambiguity, or diagnostic gap can lose one capture is
covered by this accepted-loss contract. Fix it only when it also violates the
healthy common path, a resource bound, authorization/confinement, stable
identity, or control-plane exclusion. Do not add durable state merely to close a
loss window.

### `vibe memory remember`

Keep the existing copy:

- i18n key: `memory.cli.remembered`
- English value remains `Memory queued.`; keep the existing localized values
- JSON: `ok: true` with `result.status` in `{accepted, duplicate}`

The copy describes queue admission, not storage. Do not change it to say the
fact was saved. A later product-copy change may make the loss disclaimer more
explicit without changing the writer state model.

## Current machinery removed

Delete delivery behavior concentrated in:

- `core/memory/store.py`: enqueue, claim, lease, settlement, durable flush FSM,
  `manual_required`, tombstone compaction, and processing-fault ACK surfaces
- `core/memory/coordinator.py` (`SessionFlushCoordinator`)
- `core/memory/worker.py` (`MemoryWorker`)
- `memory_capture_queue`
- `memory_session_flush_state`
- `memory_flush_settlements`
- `memory_attachment_bundle`
- `MemoryRuntime` outbox recovery and drain loops
- `MemoryModule.final_flush` waits

Do not replace them with semantically equivalent tables under observer names.

## Target writer

```text
CaptureAdmission
  existing identity / authorization / size / scope checks
        |
        v
optional existing attachment pinning
        |
        v
BestEffortMemoryWriter.offer()
  bounded local identity update + put_nowait
        |
        v
bounded asyncio.Queue + one ordered worker
        |
        v
EverOSPort.add / flush
        |
        v
best-effort summary/log update; no durable settlement
```

Suggested fixed constants:

- queue bound: 256 items
- process-local duplicate LRU: 256 source-message digests
- maximum total add attempts: 3, only for outcomes proven not to have entered
  provider execution
- idle flush: 5 minutes
- maximum unflushed age: 30 minutes
- message-count flush: 100 accumulated acknowledgements
- pending-flush tracker: 256 provider sessions, with at most 100 message ids per
  tracked session

The constants are implementation defaults, not user settings or durability
promises.

### Admission

`offer()` performs no EverOS I/O and no unbounded wait:

1. Reuse existing capture authorization and validation.
2. Reject immediately if the queue is full or the writer is not ready.
3. In one short identity transaction, enforce the named-project limit and
   allocate the next provider timestamp.
4. Put the request into the queue in the same event-loop turn.

The identity transaction uses the existing bounded SQLite policy. A busy or
failed identity write skips the capture; it does not wait indefinitely and does
not create a recovery item.

Timestamp allocation remains
`max(occurred_at_ms, last_provider_timestamp_ms + 1)`. Reject overflow before
catalog, watermark, attachment, or queue mutation. Reusing an existing named
project remains allowed at the 16-project limit; creating a 17th is rejected.

### Ordered worker and retries

One worker preserves the order of items that reach the queue. It handles one
provider operation at a time.

An add may have up to three total attempts only when the implementation can
prove the earlier attempt did not reach provider execution. Once an operation
may have executed, success, rejection, timeout, cancellation, malformed output,
or transport ambiguity is terminal for that item.

There is no durable attempt count. Restart resets all retry knowledge by
dropping the queue.

An acknowledged add updates `last_success_at` and the in-memory flush tracker on
a best-effort basis. Failure to persist a summary after provider success does
not convert the provider result into a retry and does not block later capture.

### Session flush

Keep the existing recall-freshness thresholds: 5 minutes idle, 30 minutes
maximum age, or 100 accumulated acknowledgements. These are process-local
timers, not durability boundaries.

`PendingFlush` retains only the bounded information needed to attempt a flush:
provider-session reference, raw session reference, timestamps, count, and up to
100 message ids when the current adapter requires them. The tracker is discarded
on extraction, generation replacement, or process exit.

If a 257th provider session would exceed the tracker bound, do not block or undo
an otherwise valid add. Leave that session untracked, increment a content-free
counter, and accept that its accumulated buffer may not be flushed by Avibe.

`/new` and archive enqueue one best-effort session barrier and return. At queue
head, the barrier attempts to flush currently tracked provider sessions matching
that raw session. It may miss an untracked scope, a capture still in attachment
preparation, or any state lost during a transition. It never waits for a proof
that every scope crossed a boundary.

A flush may retry only failures proven to occur before provider execution. A
submitted flush is consumed regardless of result and is never restored to the
tracker.

### Runtime replacement and shutdown

Disable, shutdown, Clear, Reset, and runtime-affecting configuration replacement
stop intake, cancel the worker, and discard queued and tracked state. They do not
drain the queue.

In-flight provider calls are not replayed. Cancellation is not treated as proof
that the provider did no work.

## Persistent identity only

After migration, `state/memory/memory.sqlite` contains only stable identity,
catalog, and small summary fields already owned by `memory_meta`:

```text
memory_meta
  singleton
  epoch
  clear_in_progress
  scope_key
  provider_root_id
  last_provider_timestamp_ms
  missed_count
  last_success_at
  last_error
  last_error_at
  updated_at

memory_projects
  principal_id
  project_id
  created_at
  last_written_at
```

Summary columns are not an event log. Updates are best-effort and may be stale.
They never gate provider submission, later capture, or runtime startup.

Do not add:

- `memory_call_provenance`
- `memory_processing_anomaly`
- `memory_flush_correlation_guard`
- `memory_observation_gap`
- any replacement per-call, per-attempt, or per-session durable table

`processing_fault_*`, `processing_alert_*`, and `processing_recovery_*` stop
being written and are not inputs to retry, Repair, or IM notification.

Provider-session derivation stays the `origin/dev` formula:

```text
src--<hmac-sha256(scope_key, "{memory_owner_id}:{project_ref}:{session_id}")>--e{epoch}
```

For capture writes, `memory_owner_id` keeps the current #1642 split: user input
uses the 34-character caller principal and agent-provenance capture uses that
caller's derived `-agent` owner. `memory_projects.principal_id` remains keyed by
the caller principal. Do not change either derivation in this phase; changing it
requires a separate migration contract.

## Processing Record and Provider Call Log

These capabilities remain, but their capture-derived views are explicitly
best-effort:

- Provider Call Log continues recording whatever its independent recorder can
  observe.
- Processing Record continues showing data that can be safely derived from
  EverOS and Provider Call Log.
- A call that cannot be authorized from retained evidence is omitted or its
  source is reported unavailable. Missing correlation must never broaden scope.
- Unlinked-call history may be incomplete or unavailable after migration,
  restart, pruning, or an ambiguous provider result.
- Recent capture failures may be shown from process-local state or the existing
  summary fields. They are lost on restart and need not reproduce the old
  newest-50 ordering.
- Diagnostics storage/read failure never blocks writer startup or provider
  submission.

The implementation should simplify the reader when it removes joins against
delivery tables. It must not introduce durable gaps, guards, anomaly ledgers, or
retention protocols to make a partial diagnostic view appear complete.

This deliberately changes the old diagnostic contract. The UI/API must label an
unavailable source truthfully rather than present an incomplete result as a
complete empty result.

## Attachments

Keep current attachment admission, pinning, and the existing single text-only
fallback. Do not redesign attachment ownership around the writer in this phase.

When queue admission or a provider attempt terminates, attempt confined bundle
release once. A failed release is logged without paths and may leave an orphan
for the next boot cleanup. It does not retain the queue item, create a retry
registry, hold writer capacity indefinitely, or stop text-only capture.

At v4 boot, run the existing confined attachment cleanup before attachment
capture is enabled. If cleanup or confinement cannot be proven, disable
attachment capture for that runtime while allowing text capture to continue.
Never follow unsafe paths or reconstruct delivery from leftover bundles.

Original chat attachments are untouched. Avibe-owned pinned copies may be
deleted or leaked until later cleanup. A later attachment PR may introduce a
narrow source-lease design if real measurements justify it.

## Migration

On-disk Memory SQLite is a shipped surface. Recognize every released v0, v1,
v2, and v3 shape already accepted by the current migrator.

Migration properties:

- recognized stores upgrade atomically to identity-only v4
- stable identity, catalog rows, timestamp watermark, and `last_success_at` are
  preserved byte-for-byte
- every delivery, retry, settlement, provenance, failure, and attachment-bundle
  row is discarded and never replayed
- unrecognized nonempty stores remain untouched and Memory stays unavailable
- Clear and Factory Reset markers remain authoritative
- a failed migration leaves the prior logical schema and rows intact

Migration outline:

1. Let the existing Factory Reset or readable Clear intent reconcile first.
2. Treat an unreadable Clear marker or an orphaned `clear_in_progress` fence as
   blocked; only an explicit Clear may supply fresh destructive authority.
3. Open the confined SQLite file and recognize its released shape without
   writing.
4. In one owned transaction, run required released-shape transforms, copy the
   stable identity/catalog fields, count discarded rows for content-free logs,
   drop all delivery tables and triggers, and install identity-only v4.
5. Commit, request a SQLite checkpoint, close every owned connection, and reopen
   normally. Never unlink SQLite WAL, SHM, or journal files directly.
6. Best-effort scrub the confined attachment pin root, then start text capture.

The migration does not copy request IDs, settlements, failure projections,
flush guards, or observation gaps. Losing that diagnostic history is accepted.

Downgrade is unsupported. An older Avibe must refuse an unknown v4 schema
without writing.

## Control plane retained

Keep:

- artifact installation and sidecar ownership
- provider-root confinement
- `MemoryOperationLease`
- Restart Engine
- Rebuild (`cascade rebuild --yes`)
- Repair (`cascade sync`)
- Clear durable intent and Factory Reset
- provider-root recreation on Clear/Reset

Before Rebuild or Repair starts a maintenance child, close writer intake and
cancel its process-local generation. Wait only for the current provider RPC to
reach the existing bounded cancellation/quiescence limit; never drain queued
captures. If safe exclusion cannot be proved, fail the maintenance operation
without launching a second provider owner.

Clear resets identity, catalog, watermark, and summaries through the existing
authority path. There are no observer tables to clear.

## Callers and receipts

Keep `CaptureRequest` and `CaptureReceipt`:

| Writer outcome | Receipt |
|---|---|
| admitted to process-local queue | `CaptureAccepted` |
| duplicate in process-local LRU | `CaptureDuplicate` |
| disabled, not ready, invalid, project limit, identity busy, or queue full | `CaptureSkipped` with the existing closed error code |

Automatic capture keeps ignoring the receipt after a content-free log.
`/internal/memory/remember` keeps returning `receipt.status`.

## Module shape

Likely result:

```text
core/memory/ingest.py          # BestEffortMemoryWriter
core/memory/identity.py        # meta + projects + summary fields
core/memory/migrate_store.py   # released-shape recognition + v4 strip
```

`coordinator.py` and `worker.py` go away. `store.py` may shrink in place instead
of being renamed when that produces a smaller diff.

`MemoryRuntime` / `MemoryModule` retain the public read and maintenance facade.
Controller and handlers must not see delivery-state primitives.

## User documentation

The implementation updates `docs/MEMORY.md` and `docs/MEMORY_ZH.md` to say:

- capture is best-effort and may be lost
- queue admission is not storage confirmation
- restart, saturation, provider failure, and upgrade can discard captures
- most healthy, unsaturated captures are still attempted normally
- Processing Record and Provider Call Log may be incomplete
- Rebuild, Repair, Clear, and Factory Reset retain their control-plane meaning

Do not ship those user-doc edits in this planning PR.

## Affected scenario contracts

Use existing capability catalogs rather than creating an isolated test matrix.

- `MEMORY-INDEP-001`: retain; hung Memory never blocks the next turn, `/new`,
  or archive.
- `MEMORY-INDEP-002`: retain the permitted stale-capture discard across a
  session transition.
- `MEMORY-INDEP-003`: rewrite the shutdown half; closing the runtime drops
  accepted volatile captures instead of draining them.
- `MEMORY-INDEP-008`: rewrite from "shutdown settles flush tasks" to "shutdown
  does not wait for capture or flush delivery".
- `MEMORY-INDEP-010`: retain the dispatch/lifecycle boundary, but do not turn it
  into a Memory delivery guarantee.
- `MEMORY-INDEP-012`: retain; processing health events stay in service logs and
  out of IM delivery.
- `MEMORY-IM-ATTACH-009` and `MEMORY-IM-ATTACH-011`: retain the one safe
  text-only fallback without adding durable pin recovery.

The implementation PR updates the matching catalogs, observations, and tests in
the same change. Any scenario that currently asserts durable replay,
manual-required recovery, exact Processing Record history, or shutdown drain is
removed or rewritten to the best-effort property.

## Validation

### Identity and migration

- Property-test every recognized released fixture: stable identity/catalog
  fields survive and every delivery-shaped row disappears without provider I/O.
- Inject migration failure at transaction boundaries and prove the old logical
  store remains loadable and unchanged.
- Prove unknown schemas and unsafe Clear authority fail closed without writes.
- Prove committed v4 reopens through SQLite's own WAL lifecycle without manual
  sidecar deletion.

### Writer

- In the normal operating envelope, an admitted text capture reaches the fake
  provider in FIFO order and an acknowledgement updates volatile flush state.
- Queue and pending-flush structures never exceed their configured bounds.
- `offer()` never awaits provider I/O; `/new`, archive, and shutdown complete
  without waiting for queued or in-flight delivery.
- Only proven-unsubmitted failures retry, within the fixed total-attempt bound;
  every possibly submitted outcome is terminal.
- Runtime replacement and restart discard all volatile capture and flush state.
- No writer path requires a durable observer write before calling EverOS.
- Diagnostics failure cannot reject an otherwise admissible capture.
- Logs and summaries remain content-free.

### Retained surfaces

- Authorization remains fail closed when Processing Record correlation is
  missing; no other principal/project data becomes visible.
- Processing Record reports unavailable/incomplete sources truthfully without
  blocking capture.
- Rebuild, Repair, Clear, and Factory Reset retain provider exclusion and marker
  authority without draining the capture queue.
- Attachment fallback remains single-shot and ambiguous provider outcomes never
  resubmit the caption.
- Updated scenario catalogs express these properties with the IDs above.

No test should assert that every capture survives. Tests should prove the common
case, explicit bounds, non-blocking behavior, security isolation, and intentional
drop behavior.

## Implementation sequence

1. Reduce v4 to identity-only schema and an atomic released-shape migrator.
2. Add `BestEffortMemoryWriter` with bounded admission, one worker, and volatile
   flush tracking.
3. Switch capture callers and session lifecycle to non-blocking offer/barrier
   calls.
4. Simplify Processing Record and Provider Call Log projections to tolerate
   missing capture correlation without broadening authorization.
5. Delete coordinator, durable worker, outbox APIs, observer protocol, and their
   state-machine tests.
6. Update scenario catalogs, focused tests, and user documentation.

Prefer one implementation PR if the resulting diff remains reviewable. If it
does not, split by a committed contract boundary, never by leaving both durable
and volatile delivery active in production.

## Todo

- [ ] Implement identity-only v4 migration.
- [ ] Implement the bounded process-local writer and flush tracker.
- [ ] Remove delivery and observer state from SQLite.
- [ ] Remove coordinator, worker, and outbox-only APIs/tests.
- [ ] Degrade diagnostics without weakening authorization.
- [ ] Keep attachment fallback without durable cleanup ownership.
- [ ] Update scenario catalogs and user documentation.

## Known-by-design

These are accepted product consequences, not defects to repair by adding durable
state:

1. Upgrade discards every undelivered outbox row and old capture-derived
   diagnostic row. EverOS data already accepted by the provider remains.
2. `CaptureAccepted` means entered process memory, not stored.
3. Restart, shutdown, disable, configuration replacement, Clear, and Reset may
   drop accepted captures, retries, and flush boundaries.
4. Queue or tracker saturation may drop captures or leave EverOS accumulation
   buffers unflushed.
5. Provider timeouts and ambiguous results are dropped without retry and may
   produce either missing data or rare duplicates.
6. Duplicate suppression resets on restart.
7. Processing Record and Provider Call Log may omit calls, failures, or
   correlations. Missing evidence never expands authorization.
8. Attachment pin/release failure may drop multimodal capture or leave an
   Avibe-owned temporary copy for later cleanup. Original chat attachments are
   untouched.

If a future product requirement needs stronger delivery or diagnostic
guarantees, design it as a separately approved capability with measured value.
Do not silently grow this writer back into a durable outbox.
