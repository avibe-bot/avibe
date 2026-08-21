# Memory best-effort capture (Phase 1)

> Status: implementation contract
>
> Scope: replace Avibe's durable Memory outbox with a process-local writer.
> The change that lands this file is docs-only. "This PR" / "implementation"
> below means the follow-up product-code PR after this contract is accepted.
> That follow-up does not rename Repair, collapse Reset, add Settings Test,
> or change EverOS.

Base: `origin/dev` at `ab91b6c07` (`feat(memory): split agent memory owner on read paths (#1633)`). Implementation rebases onto current `origin/dev` before coding.

This document is the implementation contract. The planning PR that lands it does not change product code. An unpublished umbrella draft considered later cuts (Repair rename, Processing Record, attachments, Settings Test); those remain out of scope here.

## Background

Avibe currently persists every eligible capture in SQLite
(`state/memory/memory.sqlite`) and delivers it through claims, leases,
generation fences, flush settlements, tombstones, and `manual_required`.
That protocol is a second reliability system around EverOS. Ordinary chat
does not wait for it, and processing failures no longer notify IM
administrators (`0210f154c` / #1631).

The durable outbox's real guarantee is "Avibe can replay a capture after
this process restarts." EverOS already keeps markdown and
`unprocessed_buffer` as its own durable sources. Replaying Avibe's queue
after an ambiguous add also risks duplicates.

This change makes capture best-effort and process-local, while preserving
the identity that already-released installs use to reach their EverOS
provider root.

## Goals

1. Automatic capture and `vibe memory remember` never wait on EverOS.
2. Capture payloads, retries, and flush schedules do not survive an Avibe
   process restart, disable, configuration replacement, or Reset/Clear.
3. Ambiguous EverOS add/flush outcomes are not retried.
4. `/new` and archive never wait for Memory.
5. Every released Memory SQLite shape loads. Stable identity and the
   EverOS provider root survive. Pending outbox rows are counted and
   discarded, never replayed.
6. Sidecar supervision, Rebuild, Repair (`cascade sync`), Clear, Factory
   Reset, Processing Record, Provider Call Log, and attachment pinning
   stay in place except where they exist only to serve the outbox.

## Non-goals

- No EverOS source, API, or data-format change.
- No identity-file format change. Identity stays in the existing SQLite
  file, with delivery tables removed.
- No ephemeral attachment-lease redesign. Pins remain in-process; leftover
  bundles after migration or crash are deleted, not recovered.
- No Repair/Rebuild rename, no Clear/Factory-Reset collapse, no Settings
  Test, no `cascade sync` removal.
- No change to principal, project, epoch, or provider-session derivation.
- No automatic rebuild, repair, or reset.

## Product contract

### Guarantees

- Chat, `/new`, and archive do not block on Memory.
- `offer()` / `capture()` only does bounded local work.
- The in-memory queue has a hard bound and cannot grow without limit.
- Principal, project, epoch, `scope_key`, and `provider_root_id` are
  byte-identical across the upgrade.
- Named-project catalog rows survive.
- `last_provider_timestamp_ms` survives and remains the lower bound for
  new EverOS add timestamps, so post-upgrade writes cannot reorder against
  already-stored EverOS cells.
- An in-progress Clear or Factory Reset is completed by the existing
  destructive path. The capture migrator does not strip or rewrite a store
  that those markers still own.
- A Clear marker that exists but cannot be read is still an open destructive
  fence. Migration and all Memory writes stay blocked until the existing
  Clear retry replaces or completes that marker.
- An in-progress Rebuild or Repair is not converted to Reset. Queue
  discard is allowed; the provider root is not touched. The writer cannot
  call EverOS concurrently with either maintenance child.
- Captures that reserve an exact-session admission slot before `/new` or
  archive reserve their slot are offered before that session's flush barrier,
  even when attachment pinning completes later.
- The existing per-principal limit of 16 named projects remains enforced.
  Reusing an existing named project is still allowed at the limit.
- Credentials, configured URLs, captured text, and absolute paths stay
  out of service logs.

### Explicit non-guarantees

- A successful `CaptureAccepted` means the item entered process memory.
  It does not mean EverOS stored it.
- Queue contents do not survive process crash or restart.
- A provider outage is not followed by durable replay when the provider
  recovers.
- Ambiguous add/flush results may leave a rare duplicate if EverOS
  committed and Avibe could not observe the ack. They are not retried.
- Session-boundary flush is best-effort. Queue saturation or a crash may
  leave old and new conversation content in the same EverOS accumulation
  boundary.
- Duplicate source-message suppression is process-local. A restart may
  recapture the same IM or remember payload if the caller submits it
  again.
- Pending outbox rows present at upgrade are dropped. Users may lose
  captures that had not reached EverOS.

### `vibe memory remember`

On `origin/dev` the human copy is already queued, not saved:
`memory.cli.remembered` is "Memory queued." / "记忆已加入队列。" JSON
success is `ok: true` with `result.status` in `{accepted, duplicate}`.
Keep that copy. Do not change it to claim the fact is stored in EverOS.
If product later wants a stronger disclaimer (process-local, dropped on
restart), that is a copy tweak in the implementation PR, not a status
machine change. Automatic capture callers keep ignoring the receipt
after structured logging.

## Current machinery this PR removes

Concentrated in:

- `core/memory/store.py` delivery surface: `enqueue_request`, `claim_*`,
  `settle*`, flush FSM, `manual_required`, tombstone compaction,
  processing-fault ACK
- `core/memory/coordinator.py` (`SessionFlushCoordinator`)
- `core/memory/worker.py` (`MemoryWorker`)
- `memory_capture_queue`, `memory_session_flush_state`,
  `memory_flush_settlements`, `memory_attachment_bundle`
- `MemoryModule.final_flush` / `run_session_lifecycle` waits
- `MemoryRuntime` drain loop, claim pause/resume, and boot outbox recovery

Retained in the same SQLite file:

- `memory_meta` identity columns listed below
- `memory_projects`

## Target writer

```text
CaptureAdmission (unchanged authorize/size/scope checks)
        |
        v
exact-session FIFO reservation      # registered before deferred pinning
        |
        v
optional AttachmentPinStore pin (unchanged, in-process only)
        |
        v
BestEffortMemoryWriter.offer()     # put_nowait, no await on EverOS
        |
        v
bounded asyncio.Queue + one ordered worker
        |
        v
EverOSPort.add / flush
```

Suggested starting constants, fixed rather than user settings:

- queue bound: 256 items
- max total add attempts: 3, only for outcomes that prove the request
  did not commit (UDS refused before send, sidecar not ready, provider
  error classified uncommitted)
- do not retry timeout, truncated response, malformed success, or any
  result that may have committed
- idle flush: 5 minutes (`SessionFlushCoordinator.IDLE_FLUSH_TIMEOUT`)
- max unflushed age: 30 minutes (`MAX_UNFLUSHED_AGE`)
- message-count flush: 100 (`MAX_UNFLUSHED_MESSAGES`)
- process-local duplicate LRU: 256 source-message digests

These flush timers are recall-freshness, not durability. An `accumulated`
add is already in EverOS `unprocessed_buffer`. Speeding them up is not
required for this cut.

`extracted` drops pending flush state for that provider session.
`accumulated` refreshes one in-memory `PendingFlush(session_ref,
first_accumulated_at, last_accumulated_at, message_ids)`. `message_ids` is
bounded by the 100-message flush limit and exists only to correlate a
successful flush with the retained Provider Call Log. The pending table does
not survive process restart; EverOS still holds the buffer until a later
same-session add, Repair, or provider extraction.

Worker generation increments on disable, shutdown, Reset/Clear, and
runtime-affecting configuration replacement. The old worker is cancelled,
the queue and flush table are discarded, and in-flight requests are not
replayed.

### Session boundary

`/new` and archive enqueue an ordered flush-barrier item and return.
They never call `final_flush` with a deadline. Existing internal routes
may remain as adapters that enqueue the barrier; they must not wait.

Keep the current O(1) exact-session FIFO reservation, or an equivalent
sequencer. A capture registers its reservation synchronously before its first
deferred step, including attachment pinning. A barrier registers after the
current tail and schedules its queue offer only after every predecessor has
either offered its add item or closed as skipped. Registering the barrier is
non-blocking, so `/new` and archive return without waiting for pins, provider
I/O, or the worker. Queue saturation may reject the barrier, but a barrier
that is accepted must never overtake already-started capture work.

### Shutdown

Stop intake, increment generation, cancel the worker, drop volatile
state, then continue the existing sidecar stop. No queue drain.

## Persistent identity and display provenance

Do **not** introduce a JSON identity file in this PR. Clear, Factory
Reset, Rebuild fencing, and `reset_for_clear(target_epoch=...)` already
own `memory_meta.epoch`. A second identity format would add crash windows
without removing the outbox.

After migration, `state/memory/memory.sqlite` contains identity plus one
bounded, content-free display-provenance table. It contains no delivery work,
retry state, flush schedule, captured text, or attachment path:

```text
memory_meta
  singleton
  epoch
  clear_in_progress
  scope_key
  provider_root_id
  last_provider_timestamp_ms
  missed_count          # optional; may stay as a counter, not a backlog
  last_success_at
  last_error
  last_error_at
  updated_at

memory_projects
  principal_id
  project_id
  created_at
  last_written_at

memory_call_provenance
  request_id
  operation_kind       # add | flush
  principal_id
  project_id
  session_id
  message_ids_json     # exact EverOS message ids, at most 100
  recorded_at_ms
```

`memory_call_provenance` replaces the correlation and authorization facts
that Processing Record currently reads from `memory_capture_queue` and
`memory_flush_settlements`. It is observer metadata, not an outbox:

- after an acknowledged add, write the returned request id with its one exact
  `m_<session>_<provider timestamp>_000` message id
- after an acknowledged flush, write the returned request id with the bounded
  message-id list from that session's `PendingFlush`
- retain at most 5,000 provenance rows and at most 14 days, matching Provider
  Call Log retention; enforce both bounds after each provenance write and at
  boot with the same policy values
- for a user-scoped read, authorize a request id only when all of its
  provenance rows resolve to exactly one principal/project and the memcell
  contains one of the recorded message ids; ambiguous or missing provenance
  never broadens visibility

A provenance write failure never retries an EverOS add or flush. It marks the
Processing Record capture-correlation source unavailable for that observation
and emits content-free diagnostics; ordinary capture remains best-effort.
Clear's `reset_for_clear` deletes every provenance row with the catalog and
watermark reset; Factory Reset deletes it with `state/memory`.

`processing_fault_*` / `processing_alert_*` / `processing_recovery_*`
stop being written. They may be dropped in the v4 table rebuild or left
unused; they are not an input to retry, Repair, or IM notification.

`last_provider_timestamp_ms` remains durable. Each accepted offer assigns
`max(occurred_at_ms, last_provider_timestamp_ms + 1)` and persists the
new watermark in the same identity transaction as the catalog upsert for
named projects. That is a small identity write, not an outbox.

That identity transaction preserves `MAX_NAMED_MEMORY_PROJECTS`: an existing
named-project row may be updated at the limit, but inserting a 17th distinct
named project for one principal returns the existing `project_limit` outcome,
mapped to `memory_invalid_input`. The rejected capture does not advance the
watermark, enter the writer queue, or leave a pin. The default project does not
count toward the cap.

Provider session derivation stays the `origin/dev` formula in
`core/memory/store.py::_provider_session_ref`:

```text
src--<hmac-sha256(scope_key, "{memory_owner_id}:{project_ref}:{session_id}")>--e{epoch}
```

`memory_owner_id` is the user principal `u-<32 hex>` for
`provenance="user_input"`, or `{principal}-agent` (`u-<32 hex>-agent`)
for `provenance="agent"` (`derive_assistant_memory_owner_id`, #1633).
`memory_projects.principal_id` remains the 34-character user principal;
the catalog is not keyed by the `-agent` owner. Implementation copies
these helpers. It does not change digest inputs, principal shape, or
the catalog key. Changing them would orphan existing EverOS sessions.

## Released shapes the migrator must accept

On-disk Memory SQLite is a shipped surface. v3.0.10 (2026-08-11) released
the #1217 foundation. `dev` already migrates those files up to schema v3
(`5a1a8ff3` and follow-ups). This PR adds one more step: v3 →
identity-and-provenance v4, and must still enter from every earlier released
shape.

Checked-in fixtures to drive, not a closed list of "interesting" rows:

| Fixture / recognizer | `PRAGMA user_version` | What it is |
|---|---|---|
| `tests/fixtures/memory_initial_foundation_v0.sql` | 0 | Initial foundation (`4cc4b158`): `memory_meta` + `memory_capture_queue` without `provider_session_ref` |
| `tests/fixtures/memory_foundation_v0.sql` | 0 | Later v0 (`a104df57` / v3.0.10): same tables with `provider_session_ref` |
| `_recognized_v0_shape` both branches | 0 | The two nonempty v0 column sets already implemented |
| `tests/fixtures/memory_foundation_v1.sql` | 1 | Adds bundles, flush state, settlements, recovery timestamp |
| `tests/fixtures/memory_foundation_v2.sql` | 2 | Adds `processing_fault_generation` pair |
| current `schema.sql` | 3 | Widened `project_ref` + `memory_projects` |

Empty version-zero (no application tables) continues to install the new
identity schema directly.

Unrecognized nonempty files keep today's fail-closed rule: raise, leave
the file untouched, do not start Memory writes. Avibe startup still
proceeds; Memory enters the existing unavailable/blocked projection.

Property, not an enumeration: seed one database of every recognized
released shape, including rows the production code can already persist
(pending, processing, delivered, dead, `manual_required`, named project,
default project, both provenances, attachment bundle present or absent,
flush in_flight / idle, processing-fault columns set or null, WAL+SHM
sidecars). After upgrade:

- `scope_key`, `epoch`, `provider_root_id`, and
  `last_provider_timestamp_ms` are unchanged
- `memory_projects` rows are unchanged
- no capture payload, lease, fence, settlement, or bundle row remains
- no dropped payload is sent to EverOS
- the original file is not mutated if the transaction fails

A shape added later that the recognizer accepts is covered by the same
property. A shape the recognizer rejects must remain byte-identical.

## Migration algorithm

Boot order, before the writer starts and before sidecar activation that
would accept captures:

1. If `recovery_intent == factory_reset` (or the equivalent runtime
   factory-reset pending flag): run the existing Factory Reset path.
   Do not read or rewrite `memory.sqlite` as an identity source; Reset
   deletes `memory` and `state/memory`.
2. Read `state/memory/clear-intent.json` through `ClearIntentStore.load()`.
   If it is present and readable, or `memory_meta.clear_in_progress` is set,
   run the existing Clear reconcile. Clear already deletes queue tables and
   bumps epoch. After it finishes, the store is empty identity at
   `target_epoch`. Then open it as v4, creating identity tables if Clear's
   reset still used the old schema for one boot. If the marker exists but is
   truncated, malformed, inaccessible, oversized, symlinked, or otherwise
   raises `ClearIntentUnreadable`, preserve `MemoryMaintenance.is_open()`
   semantics: do not migrate, do not start the sidecar/writer, and project the
   existing `memory_clear_marker_unreadable` retry state. Only an explicit
   Clear re-run may replace that marker.
3. Otherwise open `state/memory/memory.sqlite` through the existing
   confined path.
4. Reuse the current v0→v2→v3 chain so the in-memory connection is a
   recognized v3 (or empty). Do not skip that chain; v0/v1/v2 installs
   must not have to know about v4.
5. In one immediate transaction:
   - count nonterminal queue rows (`pending`, `processing`,
     `manual_required`) and terminal rows, without logging payload text
   - collect `attachment_bundle` ids
   - copy `memory_meta` identity columns and `memory_projects`
   - before dropping delivery tables, copy add/flush request correlation into
     `memory_call_provenance`, bounded to retained Provider Call Log request
     ids and its 5,000-row / 14-day window; preserve the current fail-closed
     rule for request ids that resolve to more than one scope
   - drop delivery tables, indexes, and settlement triggers
   - rebuild `memory_meta` without requiring delivery columns if this
     PR drops them
   - `PRAGMA user_version = 4`
   - verify identity tables and required identity columns
6. Commit, then confined-delete leftover pin bundles whose ids were
   collected, then confined-delete WAL/SHM only after the main file
   has the new user_version.
7. Emit one content-free structured log: discarded nonterminal count,
   discarded terminal count, discarded bundle count. Never log text,
   paths, or digests that can recover a message.
8. Start `BestEffortMemoryWriter` against the preserved watermark and
   catalog.

Crash windows:

- Failure before commit: old file unchanged; next boot retries.
- Failure after commit, before bundle delete: the store is v4; leftover
  files under `memory/attachments/` are unreferenced and deleted on the
  next boot by "no bundle table ⇒ delete pin root if empty-orphan".
- Failure during Clear/Reset: existing markers remain authoritative.
  The capture migrator does not clear those markers and does not treat
  a pending Clear as a successful identity upgrade.

Never:

- replay a pending row into the new writer
- bump `epoch` as a side effect of discarding the queue
- rotate `scope_key` or `provider_root_id`
- convert a pending rebuild marker into Reset
- delete the EverOS provider root
- delete Provider Call Log data

Downgrade is unsupported. A v4 file opened by an older Avibe that only
understands v3 is an unrecognized schema and must fail closed without
writing, same as today's unknown `user_version` rule.

## Attachments in this PR

Keep `AttachmentPinStore` and IM/Workbench admission. The worker still
registers the exact-session FIFO reservation before pinning, pins before
`offer` when the current path pins, and releases the bundle after a terminal
in-process add (success, definite rejection, or drop). Closing a reservation
as skipped releases its successor, so one failed pin cannot strand a later
barrier.

Remove only:

- restart recovery of pinned bundles via queue rows
- snapshot/restore handling that exists solely for durable delivery
- any assumption that a bundle outlives the process

After migration or crash, unreferenced pin directories are deleted.
That is data loss of files Avibe copied for retry, not of the user's
original chat attachments.

A later PR may replace pins with ephemeral source leases. It is not
required to land best-effort capture.

## Callers and receipts

Keep `CaptureRequest` and `CaptureReceipt`. Map writer outcomes:

| Writer | Receipt |
|---|---|
| queued | `CaptureAccepted` |
| duplicate in process-local LRU | `CaptureDuplicate` |
| disabled / not ready / invalid / named-project limit / queue full | `CaptureSkipped` with the existing closed error code |

Automatic capture still ignores the receipt after a content-free log.
`/internal/memory/remember` still returns `receipt.status`. CLI copy
stays queued, as above.

Processing-fault durable notification and ACK in `MemoryStore` /
`SessionFlushCoordinator` are deleted. IM already does not receive those
events. Processing Record may show process-local queue depth labelled
as volatile; it must not show a durable missed-capture backlog.

Processing Record and Provider Call Log otherwise retain their current
scope and correlation behavior through `memory_call_provenance`. The reader's
capture-source availability probe and request-id joins move to that table;
they do not silently omit request-id call branches merely because the
delivery tables are gone.

## Control plane that stays

The claim API goes away with the outbox, but its provider-maintenance
exclusion does not. `BestEffortMemoryWriter` exposes pause/quiesce/resume for
the existing control plane:

- artifact install, sidecar ownership, provider-root confinement
- `MemoryOperationLease`
- Restart Engine
- Rebuild (`cascade rebuild --yes`) and Repair (`cascade sync`)
- Clear durable intent and Factory Reset
- `ProviderRoot` recreation on Clear/Reset

Before Rebuild or Repair launches `cascade rebuild` / `cascade sync`, it
synchronously closes writer intake, increments the writer generation, drops
unsubmitted queue and pending-flush state, and waits until no add/flush RPC is
in flight. New captures during that interval return
`memory_operation_in_progress`. If quiescence cannot be proved, maintenance
fails before launching its child and keeps the writer fail-closed; it never
runs concurrently with a provider call. Repair opens a fresh writer generation
only after its child exits and the existing sidecar is still authoritative.
Rebuild keeps the writer paused through sidecar/root replacement and resumes
only after the replacement is admitted. This is the replacement for
`quiesce_claims()`, not an outbox drain.

Clear's queue primitive becomes: identity `reset_for_clear` (epoch bump,
catalog wipe, watermark zero, call-provenance wipe) with no capture-queue
DELETE. The other three surfaces (provider root, call log, attachments) stay.

Factory Reset still deletes `memory` and `state/memory`. The v4 store is just
another file under `state/memory`.

## Module shape after this PR

Likely:

```text
core/memory/ingest.py      # BestEffortMemoryWriter
core/memory/identity.py    # meta + projects + watermark + bounded call provenance
                           # or store.py reduced to that surface
core/memory/migrate_store.py  # v0..v3 recognition + v3→v4 strip
```

`coordinator.py` and `worker.py` go away. `store.py` may shrink in place
rather than being renamed, if that keeps Clear/Reset call sites smaller.

`MemoryRuntime` / `MemoryModule` keep the public read and maintenance
facade. Capture becomes `offer()` after admission.

## User documentation

Implementation must update `docs/MEMORY.md` and `docs/MEMORY_ZH.md`:

- delete the "durable capture queue / manual_required / final flush"
  promises
- state that capture is best-effort and dropped on restart
- state that upgrade discards undelivered outbox rows and keeps EverOS
  content
- keep Rebuild / Repair / Clear / Reinitialize descriptions except where
  they mention outbox settlement

Do not ship those doc edits in this planning PR.

## Validation

### Identity and migration

- Every checked-in released fixture opens and becomes a v4
  identity-and-provenance store.
- Property test: seed every persistable production row shape the current
  schema allows, migrate, assert identity bytes and catalog equality,
  assert zero remaining delivery tables, assert the fake provider received
  no add/flush from discarded rows.
- Unrecognized nonempty v0 remains unmodified.
- Unknown `user_version` remains unmodified.
- WAL/SHM sidecars do not come back as a second database.
- `clear_in_progress` / clear-intent / factory-reset pending skip the
  strip path and follow the existing destructive reconciler.
- Every unreadable clear-intent shape keeps migration, sidecar startup, and
  writer startup blocked without modifying the marker or SQLite identity.
- Retained add/flush request ids migrate to bounded call provenance before
  delivery tables are dropped; ambiguous cross-scope request ids remain
  unauthorized.
- Interrupted strip (kill before commit) retries and still preserves
  identity.
- v4 file is refused by a v3-only opener without writes (contract test
  with the old recognizer, or an equivalent fail-closed probe).

### Writer

- `offer()` does not await provider I/O.
- Occupancy never exceeds 256.
- Saturation returns `memory_queue_full` / `CaptureSkipped`.
- One worker preserves accepted-item order globally.
- At most three total attempts, and only for uncommitted failures.
- Timeout / unknown / truncated success is not retried.
- `accumulated` schedules idle (5m), max-age (30m), and
  message-count (100) flush in memory, matching today's coordinator.
- `extracted` removes pending flush.
- Shutdown, disable, config replacement, Clear, and Reset drop the queue
  without drain.
- Session barrier does not block `/new` or archive, and a capture delayed in
  attachment pinning after reserving admission is still offered before the
  later barrier.
- Watermark persists across writer restart and is monotonic.
- Named-project upsert still happens on accepted capture; the current
  16-project test still rejects the 17th distinct slug and accepts reuse at
  the limit without advancing rejected-capture state.
- Logs never include captured text, credentials, or absolute paths.

### Compatibility with retained surfaces

- Restart Engine still replaces only the sidecar.
- Rebuild/Repair still take the operation lease, prove the writer has no
  in-flight provider RPC before launching a child, keep intake closed for the
  operation, and resume only an admitted fresh generation.
- Clear still resumes from `clear-intent.json` and still recreates an
  empty provider root.
- Search, profile, list, remember admission, and agent-owner read paths
  keep their `origin/dev` contracts.
- Processing Record authorizes and correlates historical migrated calls and
  new add/flush calls through bounded `memory_call_provenance`, including
  scoped list/detail, unlinked-call, source-availability, retention, and
  ambiguous-request-id tests.

## Implementation sequence

1. Identity-and-provenance v4 schema + migrator + fixture property tests. No
   behavior change yet if the writer still reads v4 as empty outbox —
   prefer not to land a half-migrated store. Land migrator and writer
   together.
2. `BestEffortMemoryWriter` behind `MemoryModule.capture`.
3. Delete coordinator/worker/outbox APIs and tests that only exist for
   them.
4. Convert `final_flush` / archive / `/new` to barriers.
5. Keep the queued remember CLI copy; rewrite `docs/MEMORY.md` and
   `docs/MEMORY_ZH.md` capture-delivery sections.
6. Clear `reset_for_clear` without delivery tables.

Estimated production-code net reduction: about 5,000 lines in
`core/memory/`. Estimated test net reduction: about 7,000–9,000 lines,
after adding migrator and writer tests. Line count is not acceptance.

## Todo

- [ ] Implement v4 identity schema and released-shape migrator.
- [ ] Implement `BestEffortMemoryWriter` and switch capture to `offer()`.
- [ ] Replace capture-table Processing Record joins with bounded call
      provenance and migrate retained correlations before dropping the tables.
- [ ] Replace session final-flush waits with a best-effort barrier.
- [ ] Replace claim quiescence with writer pause/quiesce around Rebuild/Repair.
- [ ] Stop writing processing-fault ACK / `manual_required` / settlements.
- [ ] Delete leftover pin bundles after migration; keep in-process pins.
- [ ] Keep `memory.cli.remembered` as queued; rewrite Memory user docs.
- [ ] Shrink Clear's queue primitive to identity reset.
- [ ] Remove coordinator, worker, and outbox-only tests.

## Known-by-design

These are accepted product consequences, not defects to fix in the
implementation PR:

1. Upgrade discards undelivered outbox rows (`pending`, `processing`,
   `manual_required`, and terminal tombstones). EverOS markdown and
   `unprocessed_buffer` are kept. Users can lose captures that had not
   reached EverOS.
2. `CaptureAccepted` means entered process memory, not stored by EverOS.
3. Duplicate source-message suppression is an in-process LRU. Restart
   may recapture the same payload.
4. Attachment pins have no durable recovery after this cut. Leftover
   bundles are deleted. Original chat attachments are untouched.
5. Session-boundary flush is a barrier enqueue, not a wait. A crash or
   a full queue can leave consecutive conversations in one EverOS
   accumulation boundary.
6. No JSON identity file is introduced. Identity stays in the existing
   SQLite file so Clear's `reset_for_clear(target_epoch=...)` and
   Factory Reset keep one authority.
7. Pin-without-outbox is temporary debt. A later attachments PR may
   replace pins with ephemeral source leases; it is not required here.
