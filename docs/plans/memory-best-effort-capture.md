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
- `offer()` / `capture()` reserves or rejects one process-local slot without
  waiting on attachment pinning or EverOS.
- One 256-slot admission window bounds every retained capture/barrier from
  reservation through pinning, stale-generation reclamation, queueing, and the
  one in-flight provider call. There is no second reservation chain outside
  that process-wide bound.
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
- Captures admitted before `/new` or archive reserve their ordered slot are
  consumed before that session's flush barrier, even when attachment pinning
  completes later.
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
- Session-boundary flush is best-effort. Queue saturation, a crash, or dropping
  a 257th pending-session tracker may leave old and new conversation content in
  the same EverOS accumulation boundary.
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
BestEffortMemoryWriter.offer()
  # non-blocking reserve in one 256-slot ordered admission window
        |
        +--> capture slot: bounded deferred pin task -> ready or skipped
        +--> barrier slot: ready immediately
        v
one ordered worker consumes the ready head slot
        |
        v
durably open one content-free provider-call gap
        |
        v
EverOSPort.add / flush
        |
        v
record every valid request id and close that gap atomically
```

Suggested starting constants, fixed rather than user settings:

- process-wide admission-window bound: 256 total permits across
  reserved/unready, pinning (including stale work from an older generation),
  ready/queued, and the current in-flight add or barrier
- max total add attempts: 3 (`MAX_ADD_ATTEMPTS`), only for outcomes that prove
  the request did not commit (UDS refused before send, sidecar not ready,
  provider error classified uncommitted)
- max total flush attempts: 3 (`MAX_FLUSH_ATTEMPTS`), but only before the
  provider coroutine may have executed; a submitted flush is never retried
  regardless of its response
- do not retry timeout, truncated response, malformed success, or any
  result that may have committed
- idle flush: 5 minutes (`SessionFlushCoordinator.IDLE_FLUSH_TIMEOUT`)
- max unflushed age: 30 minutes (`MAX_UNFLUSHED_AGE`)
- message-count flush: 100 (`MAX_UNFLUSHED_MESSAGES`)
- pending-flush bound: 256 provider sessions
  (`MAX_PENDING_FLUSH_SESSIONS`) / 25,600 retained message ids
  (`MAX_PENDING_FLUSH_MESSAGE_IDS`)
- process-local duplicate LRU: 256 source-message digests

These flush timers are recall-freshness, not durability. An `accumulated`
add is already in EverOS `unprocessed_buffer`. Speeding them up is not
required for this cut.

`extracted` drops pending flush state for that provider session.
`accumulated` refreshes one in-memory `PendingFlush(session_ref,
raw_session_id, first_accumulated_at, last_accumulated_at, message_ids,
attempts)`. `message_ids` is bounded by the 100-message flush limit and exists
only to correlate any submitted flush outcome with the retained Provider Call
Log. `raw_session_id` is the already-validated bounded capture identifier; it
stays volatile and is never logged. The message-id list length is the volatile
`unflushed_count`: every `accumulated`
acknowledgement appends exactly one message id, and reaching 100 makes that
provider session immediately due before another add for the same session can be
submitted.

The map retains at most 256 provider sessions, so its 100-id per-entry limit
also bounds total retained ids at 25,600. If an `accumulated` result would create
a 257th entry, do not evict or mutate an existing scope: drop the new volatile
tracking entry, increment a content-free capacity-drop counter, and leave the
EverOS buffer untouched. A later same-session add may recreate tracking after
capacity is available, and Repair or provider extraction may process the older
buffer. The pending map does not survive process restart.

When an entry becomes due, the worker snapshots it for one flush attempt. A
failure proven to occur before the provider coroutine can execute may retain
that same entry for at most three total attempts. At the exact submission edge
where the provider coroutine may execute, remove the entry from the map and
carry its bounded message-id snapshot only in the in-flight slot. Success,
rejection, timeout, malformed response, cancellation, and any other ambiguous
submitted outcome all consume that snapshot permanently; none reinsert or
reschedule it. Only a later `accumulated` add can create fresh pending state.

Worker generation increments on disable, shutdown, Reset/Clear, and
runtime-affecting configuration replacement. The old worker is cancelled, the
ordered queue and pending-flush map are invalidated immediately, and in-flight
provider requests are not replayed. A synchronous pin that is already running
becomes stale cleanup work under the process-wide admission bound described
below; the generation transition does not wait for it.

### Session boundary

`/new` and archive enqueue one ordered `SessionBarrier(raw_session_id)` slot and
return. They never call `final_flush` with a deadline. Existing internal routes
may remain as adapters that enqueue the barrier; they must not wait.

The worker resolves the barrier only when it reaches the head, after every
earlier admitted capture has either updated or skipped pending state. It selects
every `PendingFlush` whose `raw_session_id` matches, including all principal /
project scopes accumulated by one Workbench session after project switches,
sorts their provider-session refs deterministically, and executes one logical
flush barrier for each inside the same charged admission slot. Later captures
cannot pass the batch. A scope with no retained pending entry needs no flush;
the explicit pending-capacity and restart non-guarantees still apply.

This head-time expansion is the barrier adapter's multi-scope replacement for
`resolve_current_session_scopes()`: resolving at adapter-call time would miss an
earlier admitted capture whose pin has not completed yet. The single batch slot
therefore carries only the trusted raw session id until it can produce the
complete ordered tuple of logical per-scope barriers.

Use one fixed-capacity ordered admission window, not a queue plus an unbounded
reservation chain. `offer()` synchronously and non-blockingly reserves the next
sequence slot before any deferred attachment work. That permit remains charged
while the slot is unready, pinning, ready, queued, or in flight. A normal slot
releases it when consumed or skipped; a stale pin keeps the permit and source
lease until late-completion reclamation finishes. At most 256 slot payloads,
attachment leases, and owned or stale pin tasks therefore exist process-wide,
including across generation replacement.

A barrier reserves the same kind of slot and is ready immediately. The worker
consumes only from the head, so an accepted barrier cannot overtake any earlier
admitted capture even if its pin is slow; a failed pin marks its slot skipped
and lets the worker advance. When all 256 permits are charged, captures and
barriers are rejected with the existing queue-full outcome before starting a
pin, retaining a payload, advancing the watermark/catalog, or creating another
task. `/new` and archive return without waiting. Generation drop marks pinning
slots skipped for ordering and releases every non-pinning permit immediately;
it does not cancel the underlying synchronous pin, release its source lease, or
return its permit before the late-pin reaper settles it.

### Shutdown

Stop intake, increment generation, cancel the worker, drop volatile
state, then continue the existing sidecar stop. No queue drain.

## Persistent identity and display provenance

Do **not** introduce a JSON identity file in this PR. Clear, Factory
Reset, Rebuild fencing, and `reset_for_clear(target_epoch=...)` already
own `memory_meta.epoch`. A second identity format would add crash windows
without removing the outbox.

After migration, `state/memory/memory.sqlite` contains identity plus bounded,
content-free display-provenance tables. They contain no delivery work,
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
  provenance_id        # local row key; request_id is deliberately non-unique
  request_id
  operation_kind       # add | flush
  outcome_class        # acknowledged | rejected | unknown
  principal_id
  project_id
  session_id
  message_ids_json     # exact EverOS message ids, at most 100
  recorded_at_ms

memory_call_provenance_gap
  gap_id               # local operation token, never sent to EverOS
  started_at_ms
  ended_at_ms          # null while the outcome is still unknown
  operation_kind       # add | flush | migration
  reason               # call_unobserved | migration_unknown
```

`memory_call_provenance` replaces the correlation and authorization facts
that Processing Record currently reads from `memory_capture_queue` and
`memory_flush_settlements`. It is observer metadata, not an outbox:

- record every syntactically valid, bounded request id observed in an EverOS
  response, whether its result is an acknowledged add/flush, `AddRejected`,
  `FlushRejected`, another terminal non-success, or an otherwise unclassifiable
  response whose id can still be validated
- an add row carries its one exact `m_<session>_<provider timestamp>_000`
  message id; a flush row carries the bounded message-id list from that
  session's `PendingFlush`
- retain at most 5,000 provenance rows and at most 14 days, matching Provider
  Call Log retention, but prune only complete request-id groups: age-prune a
  group only when its newest `recorded_at_ms` is older than the cutoff, then
  delete the oldest whole groups until the row count is at most 5,000
- never retain a subset of a request-id group. If one reused group alone exceeds
  the row limit, delete that whole group; missing provenance remains fail-closed
- enforce both bounds after each provenance write and at boot with the same
  policy values
- for every user-scoped read, require all rows for the request id to resolve to
  exactly one principal/project; ambiguous or missing provenance never
  broadens visibility
- for a call reached through a linked memcell, additionally require that owned
  memcell to contain one of the recorded message ids
- for `list_unlinked_calls` and an unlinked request-id detail, unique provenance
  scope is the authorization fact: requiring memcell membership there would
  reject the branch precisely because the call is unlinked

The single ordered worker makes correlation loss restart-safe without turning
observer state into delivery state:

1. Before every EverOS add or flush attempt, the worker durably inserts one open
   `memory_call_provenance_gap` row. It also closes any stale open row at this
   attempt's start time. If this preflight identity transaction fails, the
   provider is not called: an add is skipped, while a due flush retains its
   snapshot only for the bounded pre-submission retry policy above.
2. If the result contains a valid request id, one transaction inserts every
   provenance row for that call and deletes its open gap. This applies to
   acknowledged, rejected, and unknown classifications. A separately allowed
   add retry starts only after that observer transaction commits; a submitted
   flush is terminal. A provenance transaction failure leaves the
   already-committed gap open and never causes a provider retry.
3. If an add result has no valid request id and proves no request left Avibe, or
   a flush attempt fails before its provider coroutine can execute, close the
   gap before any permitted retry; if closing fails, drop the item. The retry
   opens a new gap. A timeout, truncated or malformed response, or other result
   without a trustworthy request id after submission leaves the gap open because
   the provider may have observed the call, and a submitted flush stays terminal.
4. At boot, close a stale open gap at boot time. Until then an open gap covers
   `[started_at_ms, infinity)`. A Processing Record call whose observation time
   overlaps any gap reports the correlation source unavailable instead of
   silently disappearing from a scoped result.

A gap is global observer state rather than a principal/project authorization
fact: no scoped reader may bypass an overlapping gap. Gap metadata is also
bounded to 14 days and 256 ranges. When it exceeds the count bound, coalesce the
oldest ranges into a wider range rather than dropping coverage. Delete a closed
range only when it is wholly older than the Provider Call Log retention cutoff;
an open range is never age-deleted. This may hide a good observation
conservatively, but cannot authorize the wrong scope. Clear's `reset_for_clear`
deletes provenance rows and gaps with the catalog and watermark reset; Factory
Reset deletes them with `state/memory`.

The independently maintained Provider Call Log is not an input to identity
migration or writer admission. Historical migration copies every valid bounded
add/flush request id from the released Memory store, including rejected rows and
every scope row for a reused id, then applies the same whole-group 5,000-row /
14-day policy using capture/settlement observation times. It does not open or
filter against the call-log database. Any observer-only correlation that cannot
be translated safely creates or widens a `migration_unknown` gap over the
retained observation horizon instead of being silently skipped. An absent,
corrupt, incompatible, or unreadable call log therefore makes the Processing
Record calls section unavailable/warned through its existing projection, but
cannot block v4 migration or capture startup.

`processing_fault_*` / `processing_alert_*` / `processing_recovery_*`
stop being written. They may be dropped in the v4 table rebuild or left
unused; they are not an input to retry, Repair, or IM notification.

`last_provider_timestamp_ms` remains durable. Under the short process-local
admission critical section, a capture first proves that capacity is available,
reads the current watermark, and computes
`max(occurred_at_ms, last_provider_timestamp_ms + 1)` with a checked upper
bound. If the candidate exceeds `MAX_PROVIDER_TIMESTAMP_MS`, return the existing
`timestamp_invalid` outcome before catalog upsert, watermark mutation, slot
charge, attachment pin, or task creation. Otherwise persist the new watermark in
the same identity transaction as the catalog upsert for named projects, charge
the ordered slot, and only then release the critical section. That section has
no attachment or provider await; the identity write is not an outbox.

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

For capture writes, `memory_owner_id` remains the 34-character user principal
`u-<32 hex>` for both `provenance="user_input"` and `provenance="agent"`, exactly
as current `MemoryStore.enqueue_request()` calls `_provider_session_ref` and
constructs `ProviderSessionRef.principal_id`. Provenance remains payload-origin
metadata; this PR does not route agent-origin captures to `{principal}-agent`.
The owner-scoped helper and `derive_assistant_memory_owner_id` added for #1633
remain available to the existing read paths but do not change this write path.
Changing capture ownership requires a separate migration contract because it
would orphan the current EverOS sessions. `memory_projects.principal_id` also
remains keyed by the 34-character user principal.

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
- a failed recognized-shape migration leaves `user_version`, logical schema,
  identity, catalog, and delivery rows unchanged; SQLite may create or update
  its own transaction sidecars while rolling back

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
3. Otherwise open `state/memory/memory.sqlite` through the existing confined
   path and detect its released shape without writing.
4. Start one outer `BEGIN IMMEDIATE`. Refactor the v0→v2, v1→v2, and v2→v3
   transforms into composable helpers that accept this transaction and never
   issue their current inner `BEGIN` / `COMMIT` / `ROLLBACK`. Standalone v3
   migration may keep a wrapper that owns a transaction, but the v4 boot path
   runs every required released-shape transform under this one owner. There is
   no committed v2 or v3 intermediate.
5. In that same outer transaction, once the connection is recognized v3 (or
   the detected v0 file was empty):
   - count nonterminal queue rows (`pending`, `processing`,
     `manual_required`) and terminal rows, without logging payload text
   - count `memory_attachment_bundle` rows for content-free discard diagnostics;
     do not treat that table as a complete filesystem inventory
   - copy `memory_meta` identity columns and `memory_projects`
   - before dropping delivery tables, copy every valid bounded add/flush request
     id into `memory_call_provenance`, including rejected operations and every
     scope row for a reused id; prune only complete request-id groups to the
     5,000-row / 14-day window and preserve ambiguity as fail-closed
   - do not open the optional Provider Call Log; if observer-only request-id or
     timestamp data cannot be translated safely, create or widen one bounded
     `migration_unknown` gap over the retained horizon and count it without
     storing source data
   - drop delivery tables, indexes, and settlement triggers
   - rebuild `memory_meta` without requiring delivery columns if this
     PR drops them
   - `PRAGMA user_version = 4`
   - verify identity tables and required identity columns
6. Commit, request `PRAGMA wal_checkpoint(FULL)` on the migration connection,
   inspect its busy result, and close every store-owned connection before
   admitting the writer. A concurrent read-only Processing Record connection
   may keep the checkpoint busy; that is safe because committed pages remain in
   SQLite's WAL and are recovered on reopen. Never unlink `-wal`, `-shm`, or
   `-journal` directly. Reopen normally and verify v4.
7. Before writer admission on this migration boot and every later v4 boot, run
   `AttachmentPinStore.clear_all()` (extended to return a content-free removed
   entry count) against the confined pin root. After the outbox table is gone,
   the authoritative reference set is empty, so inventory and remove every
   staging and bundle entry, including valid published bundles with no old table
   row and safely confined entries with unrecognized names.
   If the scrub cannot prove the root empty, keep the writer closed and retry the
   same scrub on the next boot; never follow or manually unlink an unsafe entry.
8. Emit one content-free structured log: discarded nonterminal count,
   discarded terminal count, discarded bundle-row count, and scrubbed confined
   entry count. Never log text,
   paths, or digests that can recover a message. Also count provenance rows
   covered by a migration gap and a busy post-commit checkpoint without treating
   either as a capture-startup failure.
9. Start `BestEffortMemoryWriter` against the preserved watermark and
   catalog.

Crash windows:

- Failure before commit: logical schema and rows are unchanged; next boot
  retries, and SQLite owns any rollback journal/WAL artifacts.
- Failure after commit, before checkpoint/close: SQLite recovers the committed
  v4 transaction from its WAL. No cleanup code removes those pages.
- Failure after commit, before bundle delete: the store is v4; leftover
  files under `memory/attachments/` are unreferenced and the mandatory empty-root
  scrub runs again before writer admission on the next boot.
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
- unlink SQLite WAL/SHM/journal files outside SQLite's own lifecycle

Downgrade is unsupported. A v4 file opened by an older Avibe that only
understands v3 is an unrecognized schema and must fail closed without
writing, same as today's unknown `user_version` rule.

## Attachments in this PR

Keep `AttachmentPinStore` and IM/Workbench admission. `offer()` charges one
ordered admission-window slot before pinning and starts at most one owned pin
task inside that permit. The worker does not consume that slot until pinning
marks it ready or skipped, and releases the bundle after a terminal in-process
add (success, definite rejection, or drop). Marking a slot skipped lets the
worker advance, so one failed pin cannot strand a later barrier.

Do not cancel an asyncio wrapper and assume the synchronous
`AttachmentPinStore.pin()` stopped. The writer keeps a strong reference to every
pin task in a process-wide late-pin registry. A generation change atomically
marks its slot stale and ordering-skipped, but leaves its admission permit and
source lease charged without awaiting completion. When the blocking call
settles, one completion path checks the captured generation before exposing the
result:

- a current successful pin marks its original slot ready
- a stale successful pin immediately calls the confined, idempotent
  `AttachmentPinStore.release(bundle_id)` and never enters a new generation
- a stale failure is consumed as cleanup, without text-only resubmission
- every branch releases the source lease and admission permit only after result
  handling and confined reclamation finish

The reaper consumes task exceptions and stays bounded by those unreleased
process-wide permits, so repeated configuration changes cannot accumulate more
than 256 blocking pins. Clear/Reset cleanup and the late reaper serialize through
the same `AttachmentPinStore` confinement/lock; a bundle published after logical
generation invalidation is still reclaimed. If release cannot prove deletion,
fail the replacement writer generation closed, keep that permit charged, and
retry confined cleanup rather than losing ownership of the orphan. If the
process exits before a callback runs, the mandatory empty-root scrub on the next
v4 boot removes both published and staging leftovers before accepting captures.

Retain the current single text-only degradation for a valid nonempty caption:

- if pinned-attachment verification fails before provider submission with the
  existing positively classified `AttachmentBundleInvalidError`, release the
  pin and mark the same ordered slot ready once without attachments
- if EverOS returns an attachment rejection for which
  `attachment_add_rejection_proves_no_write()` is true, release the pin and
  retry the same in-flight slot once without attachments

This is a modality fallback, not replay after ambiguity. It counts toward the
same three-total-attempt bound, never reserves a second slot, and is forbidden
for timeouts, unknown/truncated responses, generic provider rejection, or any
outcome that may have written. If the caption is empty, mark the slot skipped
instead. Preserve `MEMORY-IM-ATTACH-009` and `MEMORY-IM-ATTACH-011` rather than
deleting them with the durable worker tests.

Remove only:

- restart recovery of pinned bundles via queue rows
- snapshot/restore handling that exists solely for durable delivery
- any assumption that a bundle outlives the process

After migration or crash, every confined staging/bundle entry is deleted before
the v4 writer starts, whether or not the old table recorded it.
That is data loss of files Avibe copied for retry, not of the user's
original chat attachments.

A later PR may replace pins with ephemeral source leases. It is not
required to land best-effort capture.

## Callers and receipts

Keep `CaptureRequest` and `CaptureReceipt`. Map writer outcomes:

| Writer | Receipt |
|---|---|
| ordered slot admitted | `CaptureAccepted` |
| duplicate in process-local LRU | `CaptureDuplicate` |
| disabled / not ready / invalid / named-project limit / queue full | `CaptureSkipped` with the existing closed error code |

Automatic capture still ignores the receipt after a content-free log.
`/internal/memory/remember` still returns `receipt.status`. CLI copy
stays queued, as above.

Processing-fault durable notification and ACK in `MemoryStore` /
`SessionFlushCoordinator` are deleted. IM already does not receive those
events. Processing Record may show process-local admission-window occupancy
labelled as volatile; it must not show a durable missed-capture backlog.

Processing Record and Provider Call Log otherwise retain their current scope
and correlation behavior through `memory_call_provenance` and
`memory_call_provenance_gap`. The reader's capture-source availability probe and
request-id joins move to those tables; they do not silently omit request-id call
branches merely because the delivery tables are gone.

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
catalog wipe, watermark zero, call-provenance and gap wipe) with no
capture-queue DELETE. The other three surfaces (provider root, call log,
attachments) stay.

Factory Reset still deletes `memory` and `state/memory`. The v4 store is just
another file under `state/memory`.

## Module shape after this PR

Likely:

```text
core/memory/ingest.py      # BestEffortMemoryWriter
core/memory/identity.py    # meta + projects + watermark + bounded provenance/gaps
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
- Inject a failure after every composed v0/v1/v2/v3 transform and during the
  v4 strip; every case rolls back the one outer transaction, preserves the
  original `user_version`, schema, and rows, and retries from the same released
  shape on the next boot. No test may observe a committed v2/v3 intermediate.
- Unrecognized nonempty v0 remains unmodified.
- Unknown `user_version` remains unmodified.
- A committed v4 store reopens correctly from either checkpointed main pages or
  a retained WAL. A concurrent Processing Record reader may keep WAL/SHM alive;
  migration closes owned connections and never deletes SQLite sidecars.
- Missing, corrupt, incompatible, or unreadable Provider Call Log state does
  not fail identity migration or block writer startup; its observer projection
  reports unavailable/warned independently.
- `clear_in_progress` / clear-intent / factory-reset pending skip the
  strip path and follow the existing destructive reconciler.
- Every unreadable clear-intent shape keeps migration, sidecar startup, and
  writer startup blocked without modifying the marker or SQLite identity.
- Retained add/flush request ids migrate to bounded call provenance before
  delivery tables are dropped, including rejected calls and every scope row for
  reused ids; ambiguous cross-scope request ids remain unauthorized.
- Provenance age and row-count pruning deletes complete request-id groups only;
  it cannot turn a reused ambiguous id into a uniquely authorized id, including
  when one group alone exceeds the row bound.
- A malformed observer correlation creates a bounded retained-horizon migration
  gap; after restart, overlapping Processing Record observations still report
  correlation unavailable rather than silently disappearing.
- Migration and later-v4-boot tests seed represented bundles, an unrepresented
  valid published bundle, staging leftovers, and safely confined unrecognized
  entries, then prove the empty-reference scrub removes all of them before
  writer admission. An injected scrub failure keeps the writer closed and the
  next boot retries without relying on the removed table.
- Interrupted strip (kill before commit) retries and still preserves
  identity.
- v4 file is refused by a v3-only opener without writes (contract test
  with the old recognizer, or an equivalent fail-closed probe).

### Writer

- `offer()` does not await attachment pinning or provider I/O.
- Total occupancy never exceeds 256 across unready reservations, current and
  stale pin tasks, ready/queued slots, and the in-flight slot, even through
  repeated generation changes; there is no out-of-window predecessor chain.
- Saturation returns `memory_queue_full` / `CaptureSkipped`.
- One worker preserves accepted-item order globally.
- At most three total attempts per operation, and only for proven-uncommitted adds or
  pre-submission flush failures.
- Timeout / unknown / truncated success is not retried.
- `accumulated` schedules idle (5m), max-age (30m), and
  message-count (100) flush in memory, matching today's coordinator.
- 99 accumulated acknowledgements do not satisfy the count boundary; the
  100th makes the session immediately due, and extraction/generation drop
  clears both the bounded message-id list and its count.
- `extracted` removes pending flush.
- Every submitted flush consumes its pending entry and bounded message-id
  snapshot after success, rejection, timeout, malformed response, cancellation,
  or other ambiguity. Only a proven pre-submission failure retains it, and the
  third failed attempt drops it without a provider call.
- High-cardinality accumulated traffic retains at most 256 pending sessions and
  25,600 message ids. A 257th new session drops only its new volatile tracking,
  never evicts or mutates an existing scope, and records content-free accounting.
- Shutdown, disable, config replacement, Clear, and Reset drop the queue
  without drain.
- Session barrier does not block `/new` or archive. At its ordered head it
  resolves and flushes every retained pending scope for the raw Workbench
  session, including project switches and scopes created by an earlier capture
  whose pin completed after barrier admission.
- A Workbench project-switch archive test accumulates at least two scopes for
  one raw session and observes one ordered submitted flush per retained scope;
  the same assertion holds when the first scope's pin finishes after barrier
  admission.
- With the head pin stalled, filling all 256 permits creates at most 256 slot
  payloads/tasks/leases; the next capture and barrier are rejected before pin
  or identity mutation. An already accepted barrier remains ordered after all
  earlier slots and before every later accepted slot.
- Generation cancellation after bundle publication but before `pin()` returns
  marks the slot skipped without waiting, keeps its process-wide permit and
  source lease charged, reclaims the late bundle through confinement, and only
  then releases both. A replacement generation cannot exceed the same 256 cap.
- An injected late-bundle release failure keeps the replacement generation
  closed and its permit charged until confined cleanup succeeds; restart runs
  the mandatory empty-root scrub before reopening intake.
- Watermark persists across writer restart and is monotonic.
- With the watermark already at `MAX_PROVIDER_TIMESTAMP_MS`, the next otherwise
  valid capture returns `timestamp_invalid` without changing the watermark or
  catalog and without charging a slot, pinning, or creating a task.
- Named-project upsert still happens on accepted capture; the current
  16-project test still rejects the 17th distinct slug and accepts reuse at
  the limit without advancing rejected-capture state.
- `MEMORY-IM-ATTACH-009` and `MEMORY-IM-ATTACH-011` still perform exactly one
  safe text-only degradation, while ambiguous or unclassified attachment
  failures never resubmit the caption.
- Logs never include captured text, credentials, or absolute paths.
- Every provider attempt durably opens a gap before the RPC. A failed preflight
  write skips an add or retains a due flush only within its bounded
  pre-submission attempts, without calling EverOS; timeout/unknown responses and
  an injected provenance-commit failure leave a gap that remains fail-closed
  after restart and is closed at the next attempt or boot.
- Valid bounded request ids from acknowledged, rejected, and otherwise
  unclassifiable add/flush responses are recorded with their outcome and scope.
  Provenance failure never causes a provider retry; a separately allowed add or
  pre-submission flush retry first settles the old observer gap and opens a new
  one, while a submitted flush remains terminal.
- Gap compaction coalesces old ranges without losing covered time, and age
  deletion removes only closed ranges wholly outside Call Log retention.

### Compatibility with retained surfaces

- Restart Engine still replaces only the sidecar.
- Rebuild/Repair still take the operation lease, prove the writer has no
  in-flight provider RPC before launching a child, keep intake closed for the
  operation, and resume only an admitted fresh generation.
- Clear still resumes from `clear-intent.json` and still recreates an
  empty provider root.
- Search, profile, list, remember admission, and agent-owner read paths
  keep their `origin/dev` contracts.
- Agent-provenance capture writes retain the current user-principal
  `ProviderSessionRef` and provider-session digest; no `-agent` write owner is
  introduced by this cut.
- Processing Record authorizes and correlates historical migrated calls and
  new add/flush calls through bounded provenance and gap metadata, including
  rejected calls, linked memcell membership, unique-scope unlinked list/detail,
  restart-safe source unavailability, whole-request-id-group retention, and
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
      provenance plus durable gaps, and migrate retained correlations before
      dropping the tables.
- [ ] Replace session final-flush waits with a best-effort multi-scope session
      barrier over the bounded volatile pending-flush map.
- [ ] Replace claim quiescence with writer pause/quiesce around Rebuild/Repair.
- [ ] Stop writing processing-fault ACK / `manual_required` / settlements.
- [ ] Scrub the entire confined pin root before every v4 writer start and reap
      stale-generation pin completions under the process-wide admission bound.
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
   a full queue can leave consecutive conversations in one EverOS accumulation
   boundary. A 257th concurrently tracked provider session also drops its new
   volatile flush trigger; a later same-session add, Repair, or provider
   extraction may process that buffer.
6. No JSON identity file is introduced. Identity stays in the existing
   SQLite file so Clear's `reset_for_clear(target_epoch=...)` and
   Factory Reset keep one authority.
7. Pin-without-outbox is temporary debt. A later attachments PR may
   replace pins with ephemeral source leases; it is not required here.
