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
  reservation through pinning, current- or stale-generation bundle reclamation,
  queueing, and the one in-flight provider call. There is no second reservation
  or terminal-cleanup chain outside that process-wide bound.
- Principal, project, epoch, `scope_key`, `provider_root_id`, and
  `last_success_at` are byte-identical across the upgrade.
- Named-project catalog rows survive.
- `last_provider_timestamp_ms` survives and remains the lower bound for
  new EverOS add timestamps, so post-upgrade writes cannot reorder against
  already-stored EverOS cells.
- A readable marker-owned Clear or pending Factory Reset is completed by the
  existing destructive path. The capture migrator does not strip or rewrite a
  store that those markers still own. A fence without a readable Clear marker
  stays blocked until an explicit Clear supplies fresh destructive authority.
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
- Session-boundary flush is best-effort. Admission saturation can reject a
  barrier. Process crash/restart, disable, graceful shutdown, runtime-affecting
  configuration replacement, and Clear/Reset generation invalidation all drop
  volatile boundary candidates; when the provider buffer survives the
  transition, old and new conversation content may share one accumulation
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
BestEffortMemoryWriter.offer()
  # non-blocking reserve in one 256-slot ordered admission window
        |
        +--> capture slot: bounded deferred pin task -> ready or skipped
        +--> barrier slot: ready immediately
        v
one ordered worker consumes the ready head slot
        |
        v
reserve one bounded session-boundary candidate and mark its guard incomplete
        |
        v
durably open one content-free multi-source observation gap
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
  ready/queued, the current in-flight add or barrier, and terminal bundle
  cleanup whose confined deletion has not yet succeeded
- max total add attempts: 3 (`MAX_ADD_ATTEMPTS`), only for outcomes that prove
  the request did not commit (UDS refused before send, sidecar not ready,
  provider error classified uncommitted)
- max total flush attempts: 3 (`MAX_FLUSH_ATTEMPTS`), but only before the
  provider coroutine may have executed **after** a durable observation gap has
  opened. Observer-SQLite preflight failure is not a provider attempt and does
  not consume this bound; a submitted flush is never retried regardless of its
  response
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
attempts, correlation_complete)`. `message_ids` is bounded by the 100-message
flush limit and exists only to correlate any submitted flush outcome with the
retained Provider Call Log. `raw_session_id` is the already-validated bounded
capture identifier; it stays volatile and is never logged. The message-id list
length is the volatile `unflushed_count`: every `accumulated` acknowledgement
appends exactly one message id, and reaching 100 makes that provider session
immediately due before another add for the same session can be submitted.

The map retains at most 256 provider sessions, so its 100-id per-entry limit
also bounds total retained ids at 25,600. Before an add can cross the provider
submission edge, reserve or refresh that session's `PendingFlush`, append the
deterministic message id provisionally, retain the previous tracker snapshot in
the single in-flight slot, and durably mark its content-free correlation guard
incomplete. A guard written by the current writer instance plus its matching
live tracker can become complete only when the same add returns an observed
`accumulated` acknowledgement and the prior snapshot was complete. A guard from
an older writer generation, the global guard, or an already incomplete prior
snapshot keeps the tracker incomplete.

If a new provider session would create a 257th map entry, skip that add before
calling EverOS, release its admission slot, increment a content-free capacity
counter, and do not create provider-call provenance. Never submit a call whose
possible `unprocessed_buffer` write has no retained raw-session boundary
candidate. This removes the previous capacity case that could leave an EverOS
buffer with neither a flush trigger nor a safe correlation state.

An observed `accumulated` result commits the provisional id and its restored
completeness; `extracted` clears the whole tracker and guard because it proves a
natural boundary. A rejection or failure proven before provider execution
restores the prior snapshot (and removes a newly created empty tracker). An
ambiguous submitted add keeps the provisional id, marks the
tracker incomplete and immediately due, and is never retried. If the ambiguity
is a timeout, cancellation, transport loss, or another result without a
trustworthy response boundary, pause all later provider submissions without
invalidating the map, stop and reap the owned sidecar, close the observation
gap at that lifecycle bound, replace the sidecar, and submit this incomplete
flush before later work. A returned but semantically unknown response with a
valid request id can enqueue the same due flush immediately without sidecar
replacement. `/new` and archive still return without waiting. A process crash
or any generation-invalidating transition may lose this volatile boundary
action, but its durable guard remains incomplete unless Clear/Reset deliberately
deletes observer identity with the provider root. Failure of any required
pre-submission guard/gap write skips the add without calling EverOS.

When an entry becomes due, the worker snapshots it for one flush attempt. A
failure to commit its observer preflight does not increment `attempts`: retain
the same due entry, schedule one delayed process-local retry, and create no
additional slot or task. Only after the durable gap opens can a failure proven
to occur before the provider coroutine executes consume one of at most three
total attempts. At the exact submission edge where the provider coroutine may
execute, remove the entry from the map and carry its bounded message-id snapshot
and completeness bit only in the in-flight slot. Mark its durable guard
incomplete before submission. Success,
rejection, timeout, malformed response, cancellation, and any other ambiguous
submitted outcome all consume that snapshot permanently; none reinsert or
reschedule it. An acknowledged flush or natural `extracted` add deletes that
session's exact guard; every other result leaves it incomplete for later adds.
Only a later `accumulated` add can create fresh pending state.

Worker generation increments on disable, shutdown, Reset/Clear, and
runtime-affecting configuration replacement. The old worker is cancelled, the
ordered queue and pending-flush map are invalidated immediately, and in-flight
provider requests are not replayed. A synchronous pin that is already running
becomes stale cleanup work under the process-wide admission bound described
below; current-generation terminal release work is transferred to that same
process-wide registry. Neither cleanup owner is generation-local, and the
generation transition does not wait for it.

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
while the slot is unready, pinning, ready, queued, in flight, or reclaiming a
terminal bundle. A slot with no bundle releases it when consumed or skipped; any
successful pin keeps the same permit and source lease until confined deletion
succeeds. At most 256 slot payloads, attachment leases, pin tasks, and terminal
cleanup owners therefore exist process-wide, including across generation
replacement.

A barrier reserves the same kind of slot and is ready immediately. The worker
consumes only from the head, so an accepted barrier cannot overtake any earlier
admitted capture even if its pin is slow; a failed pin marks its slot skipped
and lets the worker advance. When all 256 permits are charged, captures and
barriers are rejected with the existing queue-full outcome before starting a
pin, retaining a payload, advancing the watermark/catalog, or creating another
task. `/new` and archive return without waiting. Generation drop marks pinning
slots skipped for ordering and releases queue permits that own neither a pin nor
terminal bundle cleanup; it does not cancel the underlying synchronous pin,
release a bundle's source lease, or return its permit before the reaper proves
confined deletion.

### Shutdown

Stop intake, increment generation, cancel the worker, and drop volatile queue /
pending-flush state, then continue the existing sidecar stop. The process-wide
pin/release registry retains its charged cleanup owners until deletion succeeds
or the process exits; a later v4 boot scrubs leftovers before intake. No queue
drain.

## Persistent identity and display provenance

Do **not** introduce a JSON identity file in this PR. Clear, Factory
Reset, Rebuild fencing, and `reset_for_clear(target_epoch=...)` already
own `memory_meta.epoch`. A second identity format would add crash windows
without removing the outbox.

After migration, `state/memory/memory.sqlite` contains identity plus bounded,
content-free observer tables. They contain no delivery work, retry state, flush
schedule, captured text, or attachment path:

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
  next_anomaly_sequence # checked monotonic observer-order allocator
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
  correlation_complete # true for adds; flush snapshot completeness
  provider_started_at_ms # preflight time at the provider-submission edge
  recorded_at_ms

memory_processing_anomaly
  anomaly_id           # stable opaque local id
  kind                 # current closed MemoryFailureKind
  occurred_at_ms
  error_code           # closed Memory error code or null
  request_id           # bounded provider id or null
  attempts             # bounded add/flush attempt count
  state
  operation_kind       # add | flush
  worker_generation
  principal_id
  project_id
  order_sequence       # local, order-preserving tie breaker; never displayed
  expires_at_ms        # queue-backed abandonment + 90 days; null for settlement evidence

memory_flush_correlation_guard
  guard_key            # HMAC of provider-session ref, or one global sentinel
  writer_instance_id   # opaque generation token; null for global sentinel
  incomplete           # boolean; true cannot become complete by later adds
  recorded_at_ms

memory_observation_gap
  gap_id               # local operation token, never sent to EverOS
  started_at_ms
  ended_at_ms          # null while the outcome is still unknown
  operation_kind       # add | flush | migration
  correlation_incomplete # boolean source-availability flag
  anomaly_incomplete   # boolean source-availability flag
  reason               # call_unobserved | migration_unknown | retention_pruned
```

`memory_call_provenance` replaces the correlation and authorization facts
that Processing Record currently reads from `memory_capture_queue` and
`memory_flush_settlements`. It is observer metadata, not an outbox:

- record every syntactically valid, bounded request id observed in an EverOS
  response, whether its result is an acknowledged add/flush, `AddRejected`,
  `FlushRejected`, another terminal non-success, or an otherwise unclassifiable
  response whose id can still be validated
- an add row carries its one exact `m_<session>_<provider timestamp>_000`
  message id and `correlation_complete=true`; a flush row carries the bounded
  message-id list from that session's `PendingFlush` plus its completeness bit.
  Both carry the operation's preflight `provider_started_at_ms`, captured before
  the provider coroutine can execute
- retain at most 5,000 provenance rows and at most 14 days, matching Provider
  Call Log retention, but prune only complete request-id groups. Before
  row-count pruning any group that can still overlap retained Call Log rows,
  create or widen a closed correlation-only `retention_pruned` observation gap
  from the minimum deleted `provider_started_at_ms` through the maximum
  `recorded_at_ms` in the same transaction. This covers Call Log rows timestamped
  at request start rather than response observation. A group wholly older than
  the Call Log cutoff needs no new coverage; then delete oldest whole groups
  until the row count is at most 5,000
- never retain a subset of a request-id group. If one reused group alone exceeds
  the row limit, delete that whole group; missing provenance remains fail-closed
- enforce both bounds after each provenance write and at boot with the same
  policy values. If coverage cannot be committed, do not prune; a failed
  provider-observation transaction leaves its preflight gap open
- for every user-scoped read, require all rows for the request id to resolve to
  exactly one principal/project; ambiguous or missing provenance never
  broadens visibility
- require every row in a request-id group to have `correlation_complete=true`
  for correlation and authorization. A false flush row makes that call's
  correlation source unavailable; its later message ids must not make an
  incomplete request appear linked or unlinked
- for a call reached through a linked memcell, additionally require that owned
  memcell to contain one of the recorded message ids
- for `list_unlinked_calls` and an unlinked request-id detail, unique provenance
  scope is the authorization fact: requiring memcell membership there would
  reject the branch precisely because the call is unlinked

`memory_flush_correlation_guard` is observer completeness metadata, not a flush
schedule or delivery queue. It contains no message ids, raw session id, payload,
count, deadline, attempt, or work item:

- keep at most 256 exact session digests plus one singleton global-incomplete
  sentinel; never evict an exact guard and thereby turn unknown correlation into
  apparently complete correlation
- create/update an exact guard in the pre-submission transaction for every add
  that has reserved a live boundary candidate. Its opaque writer-instance token
  proves completeness only while the matching generation and `PendingFlush`
  still exist; the same observed `accumulated` settlement may restore the prior
  complete state, but a later unrelated add cannot
- at generation replacement or process restart, any surviving exact guard has
  an old token and therefore taints later recreated tracking as incomplete
- when exact-guard capacity is exceeded by retained old-generation rows, set the
  global sentinel. While it exists every newly created tracker is incomplete,
  including sessions not represented by an exact row. Pending-map capacity is
  handled separately by skipping a new-session add before its provider RPC
- delete an exact guard only after a natural `extracted` add or acknowledged
  flush proves that provider buffer crossed a boundary. A rejection or
  proven-uncommitted failure restores the pre-add guard snapshot; timeout,
  malformed response, cancellation, and ambiguity retain it as incomplete
- the global sentinel is cleared only by Clear/Factory Reset or a future
  provider-wide operation that positively proves every unprocessed buffer empty;
  ordinary add/flush results and sidecar restart cannot prove that

`memory_processing_anomaly` replaces the sanitized failure projection that
`MemoryStore.failure_log()` currently derives from terminal
`memory_capture_queue` rows and `memory_flush_settlements`. It is observer state,
not a retry or delivery ledger:

- write one row only when a logical capture or flush terminates rejected or
  ambiguous, or when a proven-uncommitted logical operation exhausts its bounded
  attempts. A positively classified attachment rejection that enters the one
  allowed text-only fallback is nonterminal: record its call provenance and
  settle its observation gap, but create no anomaly unless the fallback itself
  terminates unsuccessfully
- preserve the current `MemoryFailureLogEntry` fields and closed values: stable
  id, kind, occurrence time, closed error code, bounded request id, attempts,
  state, operation, and worker generation. New ids are an HMAC of `scope_key`
  plus the opaque local operation token; migrated rows retain the current
  `_failure_anomaly_id` result derived from their queue/settlement namespace and
  evidence. Principal/project columns provide authorization but are not added to
  the user-visible anomaly payload
- never store provider messages, raw exceptions, payload/message text, session
  ids, source digests, attachment metadata, paths, or lease/fence tokens
- set `expires_at_ms = occurred_at_ms + 90 days` for queue-backed
  `delivery_abandoned` rows that the current terminal-tombstone compactor
  removes, and null for settlement-backed rejected/ambiguous evidence that the
  current immutable settlement table retains. Migration derives this from the
  source side of the released `failure_log()` union, not from `kind` alone
- delete expired rows first, then apply independent caps: retain the newest
  100,000 expiring rows and the newest 100,000 non-expiring rows. Newer expiring
  evidence can never evict permanent settlement evidence that must reappear
  after those temporary rows expire; within the expiring class, an older row
  also expires no later than every newer row that displaced it. The table is
  therefore bounded to 200,000 rows total
- allocate `order_sequence` monotonically in the anomaly insert transaction and
  order/prune by `(occurred_at_ms, order_sequence)`. Migration assigns sequences
  in the released projection's existing `(occurred_at, sort_key)` order, where
  queue digests and zero-padded settlement ids are used only while reading the
  old tables and are not copied. This preserves the exact newest-50 tie order
  without retaining source digests; return that newest 50 through the unchanged
  `failure_log()` / Processing Record projection
- enforce expiry/count retention after each insert and at boot; an anomaly
  read/schema failure marks that Processing Record source unavailable rather
  than returning an empty successful list

The single ordered worker makes every observer loss explicit without turning
observer state into delivery state:

1. Before every EverOS add or flush attempt, one preflight transaction marks the
   exact correlation guard incomplete and inserts one open
   `memory_observation_gap` for that operation token with both
   `correlation_incomplete` and `anomaly_incomplete` set. For an add, the
   volatile provisional boundary reservation above happens first and is rolled
   back if preflight fails. A gap never closes or replaces another attempt's
   gap. If preflight fails, the provider is not called: an add is skipped, while
   a due flush retains the same snapshot and retries after a process-local delay
   without incrementing its provider-attempt counter. It cannot be dropped as an
   exhausted operation because no durable anomaly/gap authority exists yet.
2. If a result contains a valid request id, one transaction inserts every
   provenance row for that call with the preflight provider-start time, inserts
   its source-expiring sanitized anomaly only when the logical capture/flush is
   now terminal and unsuccessful, applies the guard and provisional-boundary
   transition, and deletes only that operation token's gap. An acknowledged add
   also advances `memory_meta.last_success_at` to the observation time in this
   transaction. This applies to acknowledged, rejected, and semantically unknown
   classifications. A positive attachment rejection that starts text fallback
   records provenance and deletes its gap without an anomaly; the fallback opens
   a new gap and only its final logical outcome can create one. Any separately
   allowed add retry starts only after that observer transaction commits; a
   submitted flush is terminal. Transaction failure leaves the preflight gap
   carrying both source flags, keeps any possible add boundary candidate
   incomplete and due, closes later provider submission, and never retries an
   ambiguously submitted provider call.
3. If an add result proves no request left Avibe, or a flush attempt fails before
   its provider coroutine can execute, restore the add's prior boundary snapshot
   and delete only that already-durable gap before any permitted retry. On the
   final bounded failure, insert the queue-backed sanitized anomaly with its
   90-day expiry in the same transaction. If that settlement transaction fails,
   the preflight gap remains durable with both source flags, so dropping the item
   is fail-closed. A retry opens a new independent gap.
4. A timeout, truncated or malformed response, cancellation, or other submitted
   result without a trustworthy request id inserts a settlement-backed
   `result_unknown` anomaly when possible and atomically clears only that gap's
   `anomaly_incomplete` flag, but leaves its correlation flag and interval open
   because the provider may still execute. The add's provisional boundary stays
   incomplete and due as described above. Later attempts do not shorten the gap
   and the provider call is never retried. Failure to insert the anomaly leaves
   both preflight flags set rather than returning a successful empty list.
5. Set `ended_at_ms` on open `call_unobserved` gaps only at a
   provider-lifecycle upper bound: intake is paused, the writer has no new
   submission edge, and the sidecar supervisor has successfully stopped and
   reaped the owned provider process. Only then may a replacement sidecar accept
   calls and the retained ambiguous-add boundary flush run before later work.
   Failed or unprovable termination leaves gaps open and intake closed. An Avibe
   boot does not close them merely because the service restarted; it may close
   them only after the same provider-termination proof. Closing a gap bounds its
   interval; it does not clear either missing-source flag.

A Processing Record call whose observation time overlaps a
`correlation_incomplete` gap reports the correlation source unavailable instead
of silently disappearing from a scoped result. Thus an older timed-out request
or row-count-pruned provenance group remains covered even if newer observations
settle successfully.

The anomaly projection is unavailable when an `anomaly_incomplete` gap is open,
or when fewer than the maximum supported failure-log limit (100) of
**non-expiring** anomaly rows are strictly newer than a closed gap's
`ended_at_ms`. Expiring queue-backed rows never retire a gap: they can disappear
later and expose missing permanent evidence again. Once 100 newer permanent rows
exist, no missing event in that older interval can affect any supported newest-N
result, so the anomaly flag may be retired. Lifecycle close alone never turns a
missing anomaly into a complete successful projection.

An observation gap is global observer state rather than a principal/project
authorization fact: no scoped reader may bypass an affected source. Keep at
most 256 ranges. When the bound would be exceeded, coalesce the oldest closed
ranges, OR their source flags, and widen their time coverage rather than
dropping a hole. Never merge away the exact token for an open provider attempt;
if no closed range can make capacity, fail preflight or retention pruning before
the provider call or provenance deletion. Every Processing Record call query
applies the same `started_at_ms >= retention_cutoff_ms` predicate before joins or
availability checks, regardless of whether background maintenance deleted older
physical rows. A closed correlation flag may retire only when that read cutoff
excludes its entire range **and** a read-only Call Log query proves no physical
`provider_call` row overlaps the range. An unavailable/busy source or remaining
stale row delays retirement but never writer admission. An open flag never
age-deletes. Delete the row only after both source flags retire. This may hide a
good observation conservatively, but cannot authorize or display an incomplete
source as complete. Clear's `reset_for_clear` deletes provenance rows, anomalies,
correlation guards, and observation gaps with the catalog and watermark reset;
Factory Reset deletes them with `state/memory`.

The independently maintained Provider Call Log is not an input to identity
migration or writer admission. Historical migration copies every valid bounded
add/flush request id from the released Memory store, including rejected rows and
every scope row for a reused id, then applies the same whole-group 5,000-row /
14-day policy. Because released rows have only capture/settlement observation
times, migration assigns the Call Log retention cutoff as their conservative
provider start; count-pruned coverage therefore still reaches every retained
call that could precede its response. It does not open or filter against the
call-log database. Any
observer source that cannot be translated safely creates or widens a
`migration_unknown` observation gap with the affected source flags over the
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

`last_success_at` also remains durable display/maintenance state. Every
acknowledged v4 add updates it in the same observer transaction as provenance and
observation-gap settlement, using the later of its existing value and the
observation time.
Provenance retention never clears it. The reduced
`has_provider_data_history()` reads this field instead of delivery rows, so
`MemoryMaintenance.maintenance_payload().data_exists` remains true after
provenance ages out while EverOS may still contain captured data. Clear resets it
to null in the same identity reset transaction; migration preserves its exact
released value.

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
identity-and-observer v4, and must still enter from every earlier released
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

- `scope_key`, `epoch`, `provider_root_id`, `last_provider_timestamp_ms`, and
  `last_success_at` are unchanged
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
   - If it is present and readable, run the existing marker-owned Clear
     reconcile. After it finishes, the store is empty identity at
     `target_epoch`. Then open it as v4, creating identity/observer tables if
     Clear's reset still used the old schema for one boot.
   - If it is absent but `memory_meta.clear_in_progress` is set, do not call
     `reconcile_pending()` and do not assume deletion occurred. Preserve the
     existing `orphaned-fence` / `memory_clear_failed` projection, keep
     migration plus sidecar/writer admission blocked, and allow only an explicit
     operator Clear to create a fresh intent and own the destructive sweep.
   - If it is truncated, malformed, inaccessible, oversized, symlinked, or
     otherwise raises `ClearIntentUnreadable`, preserve
     `MemoryMaintenance.is_open()` semantics: do not migrate, do not start the
     sidecar/writer, and project the existing
     `memory_clear_marker_unreadable` retry state. Only an explicit Clear re-run
     may replace that marker.
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
     scope row for a reused id. Released rows have no provider-start field, so
     set their conservative `provider_started_at_ms` to the migration instant's
     Call Log retention cutoff without opening that optional database. Prune
     only complete request-id groups to the 5,000-row / 14-day window, create
     correlation-only `retention_pruned` coverage from the earliest provider
     start through latest observation before count-pruning groups still inside
     the Call Log horizon, and preserve ambiguity as fail-closed
   - run the released-shape equivalent of the current `failure_log()` union and
     copy every projected queue/settlement anomaly into
     `memory_processing_anomaly` before its source tables are dropped. Preserve
     the stable id and every sanitized `MemoryFailureLogEntry` field, deduplicate
     rejected adds exactly as the current projection does, and enumerate the
     released union in ascending `(occurred_at, sort_key)` order to assign local
     `order_sequence` values before discarding the old queue digest / settlement
     id sort evidence. Map errors through the same closed-code sanitizer, set
     90-day expiry only for queue-backed abandonment rows, keep
     settlement-backed expiry null, delete rows already expired at migration
     time, and prune each expiry class independently beyond its newest 100,000
     rows. Set `next_anomaly_sequence` after the largest assigned value
   - for every old `memory_session_flush_state` row with an unflushed provider
     buffer, create an incomplete exact `memory_flush_correlation_guard` using
     only its bounded session-ref digest. If more than 256 distinct rows exist,
     keep 256 exact guards and set the global-incomplete sentinel; never migrate
     a schedule, count, deadline, fence, attempt, or message id
   - do not open the optional Provider Call Log; if observer-only request-id or
     anomaly data cannot be translated safely, create or widen one bounded
     `migration_unknown` observation gap with the affected source flags over the
     retained horizon and count it without storing source data
   - drop delivery tables, indexes, and settlement triggers
   - rebuild `memory_meta` without requiring delivery columns if this
     PR drops them
   - `PRAGMA user_version = 4`
   - verify identity and observer tables plus every required v4 column
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
   discarded terminal count, migrated anomaly count, discarded bundle-row
   count, migrated exact-guard count, global-incomplete-guard state, and
   scrubbed confined entry count. Never log text,
   paths, or digests that can recover a message. Also count provenance rows
   covered by retention/migration observation gaps, source-specific expired
   anomaly rows, and a busy post-commit checkpoint without treating any as a
   capture-startup failure.
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
marks it ready or skipped. After a terminal in-process add (success, definite
rejection, or drop), it transfers any bundle plus the same permit and source
lease to the process-wide cleanup registry before advancing. Marking the ordered
slot consumed or skipped does not release that ownership, so one failed release
cannot become an unbounded orphan and one failed pin cannot strand a later
barrier.

Each charged permit has one owner with separate logical-slot-terminal and
bundle-deleted completion bits. A positive attachment rejection may transfer the
bundle to cleanup and continue its text fallback under that same permit; the
permit and source lease return only after both bits are true. The registry holds
at most one cleanup entry per permit and one shared reaper services those
entries, so fallback does not create a second task/reservation chain.

Do not cancel an asyncio wrapper and assume the synchronous
`AttachmentPinStore.pin()` stopped. The writer keeps a strong reference to every
pin task and terminal-release entry in one process-wide cleanup registry. A
generation change atomically marks its slot stale and ordering-skipped, but
leaves its admission permit and source lease charged without awaiting completion.
When the blocking call settles, one completion path checks the captured
generation before exposing the result:

- a current successful pin marks its original slot ready
- a stale successful pin immediately calls the confined, idempotent
  `AttachmentPinStore.release(bundle_id)` and never enters a new generation
- a stale failure is consumed as cleanup, without text-only resubmission
- every current or stale terminal bundle enters the same idempotent confined
  release loop; only successful deletion releases the source lease and admission
  permit

The reaper consumes task exceptions and retries every current- or
stale-generation release while keeping its strong owner and original permit. It
stays bounded by those unreleased process-wide permits, so repeated terminal
release failures or configuration changes cannot accumulate more than 256 pins
and cleanup tasks. Clear/Reset cleanup and the reaper serialize through the same
`AttachmentPinStore` confinement/lock; a bundle published after logical
generation invalidation is still reclaimed. If stale release cannot prove
deletion, fail the replacement writer generation closed; every release failure
keeps its permit charged and retries confined cleanup rather than losing
ownership of the orphan. If the process exits before a callback runs, the
mandatory empty-root scrub on the next v4 boot removes both published and
staging leftovers before accepting captures.

Retain the current single text-only degradation for a valid nonempty caption:

- if pinned-attachment verification fails before provider submission with the
  existing positively classified `AttachmentBundleInvalidError`, release the
  pin and mark the same ordered slot ready once without attachments
- if EverOS returns an attachment rejection for which
  `attachment_add_rejection_proves_no_write()` is true, release the pin and
  retry the same in-flight slot once without attachments; the same permit owns
  both the fallback and any still-pending confined release

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
`memory_observation_gap`, with completeness guarded by
`memory_flush_correlation_guard`. The reader's capture-source availability probe
and request-id joins move to those tables; they do not silently omit request-id
call branches merely because the delivery tables are gone or a volatile
message-id set was lost. Count-pruned provenance inside the Call Log horizon is
covered by a correlation-only gap before deletion. Every Processing Record
provider-call list/detail query also applies the recorder's current 14-day
`started_at_ms` cutoff itself. Delayed/failed maintenance may leave older rows
physically readable, but those rows cannot outlive provenance/gap coverage in
the projection.

Processing Record's durable anomaly source moves independently to
`memory_processing_anomaly`. `MemoryStore.failure_log()` keeps its current
newest-50 result shape, source-specific expiry, and source-availability
behavior; it no longer queries a delivery table. Any observation gap that could
change the requested newest-N result makes that source unavailable instead of
returning an incomplete list. Disabling Memory still permits the existing
read-only retained anomaly projection.

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
catalog wipe, watermark and `last_success_at` reset, and atomic deletion of
`memory_call_provenance`, `memory_processing_anomaly`,
`memory_flush_correlation_guard`, and `memory_observation_gap`) with no
capture-queue DELETE. The other three surfaces (provider root, call log,
attachments) stay. Replaying the same `target_epoch` is idempotent and still
proves every observer table empty before the Clear fence may be released.

Factory Reset still deletes `memory` and `state/memory`. The v4 store is just
another file under `state/memory`.

## Module shape after this PR

Likely:

```text
core/memory/ingest.py      # BestEffortMemoryWriter
core/memory/identity.py    # meta + projects + watermark + bounded observer tables
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
  identity-and-observer store.
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
- A readable clear-intent or factory-reset pending state skips the strip path
  and follows its existing destructive reconciler. `clear_in_progress` without
  a readable intent skips the strip path but stays blocked as an orphaned fence;
  it never enters the destructive reconciler without fresh operator authority.
- Every unreadable clear-intent shape keeps migration, sidecar startup, and
  writer startup blocked without modifying the marker or SQLite identity.
- Retained add/flush request ids migrate to bounded call provenance before
  delivery tables are dropped, including rejected calls and every scope row for
  reused ids; ambiguous cross-scope request ids remain unauthorized. Migrated
  rows receive the Call Log cutoff as their conservative provider-start time,
  while new rows retain the exact preflight start.
- Released unflushed-session rows migrate to incomplete, content-free
  correlation guards without message ids or schedule state. At 256 distinct
  exact guards the next distinct row sets the global-incomplete sentinel; a
  later recreated tracker cannot claim complete flush provenance.
- Across the released-fixture matrix, seed every failure form each shape can
  represent, together covering `dead`, `manual_required`, boot recovery,
  rejected add/flush, and ambiguous settlement evidence. The newest-50
  `failure_log()` projection is field-for-field identical before and after
  migration (including stable ids, deduplication, closed errors, attempts,
  operation, state, generation, and bounded request ids), while the v4 file has
  no delivery tables. Queue-backed abandoned rows receive their exact 90-day
  expiry, settlement-backed rows remain unexpired, rows already expired at the
  migration instant disappear, and count retention keeps the independently
  newest 100,000 rows in each expiry class without payload or retry state. More
  than 50 queue and settlement rows sharing one occurrence timestamp retain the
  released digest / zero-padded-ID tie order through local sequences, without
  copying either old sort key.
- An old non-expiring settlement anomaly followed by 100,001 newer expiring
  queue anomalies survives the independent permanent cap. After the clock moves
  past every queue expiry, that settlement row re-enters the newest-50 result in
  its original order.
- Provenance age and row-count pruning deletes complete request-id groups only;
  it cannot turn a reused ambiguous id into a uniquely authorized id, including
  when one group alone exceeds the row bound. Count-pruning a group still inside
  the Call Log horizon atomically creates correlation-only coverage from the
  earliest provider start, not merely the response observation; a retained call
  that started earlier than `recorded_at_ms` still reports unavailable rather
  than disappearing.
- A malformed observer correlation or anomaly creates a bounded retained-horizon
  migration observation gap with the exact affected source flags; after
  restart, each overlapping/affected Processing Record source still reports
  unavailable rather than silently succeeding with omissions.
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
  stale pin tasks, current/stale terminal cleanup entries, ready/queued slots,
  and the in-flight slot, even through repeated generation changes; there is no
  out-of-window predecessor or cleanup chain.
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
  or other ambiguity. Observer preflight write failure retains it without
  consuming an attempt. After a durable gap opens, the third proven
  provider-preexecution failure drops it only after the final anomaly/gap
  settlement commits or leaves that existing gap open fail-closed.
- High-cardinality accumulated traffic retains at most 256 pending sessions and
  25,600 message ids. A capture for a 257th distinct session is skipped before
  any EverOS RPC, never evicts or mutates an existing scope, records only
  content-free accounting, and cannot create an untracked provider buffer.
- Exact correlation guards from a previous writer instance/generation taint a
  recreated tracker after restart; a global guard taints every new tracker.
  Natural extraction or acknowledged flush clears an exact guard; rejection or
  proven-uncommitted failure restores the prior snapshot; ambiguous results and
  sidecar restart do not make it complete. No test can recover complete
  provenance using only message ids accumulated after an incomplete guard.
- Before every submitted add, the deterministic message id is provisionally
  present in a barrier-visible tracker and the durable guard is incomplete. An
  accumulated ack commits it, extraction clears the boundary, and rejection or
  proven-uncommitted failure restores the exact prior snapshot.
- A timed-out/transport-ambiguous submitted add retains that provisional tracker
  incomplete and due, blocks all later provider submissions, and survives the
  sidecar stop/reap operation in process. After the lifecycle upper bound, its
  incomplete flush runs before later work; a `/new` barrier admitted meanwhile
  returns immediately, and the candidate stays barrier-visible until the
  recovery flush reaches its submission edge. If that flush cannot prove a
  boundary, its durable guard remains incomplete. A crash/restart, disable,
  graceful shutdown, runtime-affecting config replacement, or Clear/Reset
  generation invalidation may drop the volatile flush action; no ordinary
  transition can make later correlation complete.
- Shutdown, disable, config replacement, Clear, and Reset drop the queue
  without drain.
- A parameterized ambiguous-candidate transition test covers process restart,
  disable/re-enable, graceful shutdown/start, and runtime config replacement:
  each drops the volatile candidate but preserves an old/incomplete guard, so a
  recreated tracker cannot claim complete correlation. Clear/Reset may delete
  the guard only in the same successful operation that replaces/deletes the
  provider root; an interrupted operation keeps its existing fence closed.
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
- An injected current-generation terminal bundle-release failure transfers to
  the strong cleanup registry, keeps its original permit and source lease
  charged, retries confined deletion, and cannot accumulate a 257th cleanup
  owner. The same late-bundle failure keeps a replacement generation closed;
  restart runs the mandatory empty-root scrub before reopening intake.
- Watermark persists across writer restart and is monotonic.
- Every acknowledged add advances durable `last_success_at` atomically with its
  provenance/observation-gap settlement. After all call-provenance groups age
  out, `has_provider_data_history()` and maintenance `data_exists` remain true
  from that timestamp; rejected/ambiguous adds do not advance it and Clear
  resets it.
- With the watermark already at `MAX_PROVIDER_TIMESTAMP_MS`, the next otherwise
  valid capture returns `timestamp_invalid` without changing the watermark or
  catalog and without charging a slot, pinning, or creating a task.
- Named-project upsert still happens on accepted capture; the current
  16-project test still rejects the 17th distinct slug and accepts reuse at
  the limit without advancing rejected-capture state.
- `MEMORY-IM-ATTACH-009` and `MEMORY-IM-ATTACH-011` still perform exactly one
  safe text-only degradation, while ambiguous or unclassified attachment
  failures never resubmit the caption. The positively rejected attachment call
  records provenance but no terminal anomaly when its text fallback succeeds;
  only an unsuccessful final logical outcome enters `failure_log()`. An injected
  bundle-release failure during that successful fallback leaves the same permit
  and cleanup entry charged until confined deletion succeeds.
- Logs never include captured text, credentials, or absolute paths.
- Every provider attempt durably opens a two-source observation gap before the
  RPC. A failed preflight write skips an add; a due flush retains its exact
  snapshot, consumes no provider attempt, and has at most one delayed retry
  scheduled, without calling EverOS. Three consecutive preflight failures still
  retain one snapshot; after storage recovers, the gap commits before the first
  counted provider attempt. Timeout/unknown responses and an injected
  observer-commit failure leave both correlation and anomaly unavailable after
  restart. A later successful attempt settles only its own gap; the older
  ambiguous gap stays open until a paused writer plus successful sidecar
  stop/reap proves its termination boundary.
- Valid bounded request ids from acknowledged, rejected, and otherwise
  unclassifiable add/flush responses are recorded with their outcome and scope;
  only final logical non-success and exhausted proven-uncommitted operations
  also persist the current sanitized `MemoryFailureLogEntry` fields and
  source-specific expiry in the bounded anomaly table. Observer failure never
  causes an ambiguous provider retry; its preflight anomaly flag makes
  `failure_log()` unavailable even after lifecycle close until 100 newer
  non-expiring known rows exclude the hole from every supported newest-N result.
  Expiring rows never retire that flag. A separately allowed add or
  pre-submission flush retry settles only its own proven-unentered gap and opens
  a new one, while a submitted flush remains terminal.
- An ambiguous request A prevents request B from reaching the provider until a
  successful owned-sidecar stop/reap closes A's interval. The replacement then
  flushes A's incomplete candidate before B can submit and delete only B's gap;
  A's closed correlation flag still covers late Provider Call Log rows until
  retention. A failed stop or unproved boot admits no B provider submission.
- Observation-gap compaction coalesces old ranges with the union of their source
  flags and without losing covered time. Correlation flags retire only outside
  the cutoff enforced by every Processing Record call query and after a
  read-only Call Log probe proves no covered physical row remains; anomaly flags
  retire only after 100 newer non-expiring known anomalies make the interval
  irrelevant to every supported newest-N result. Expiring newer rows may
  disappear and therefore never retire a gap.
- With Call Log maintenance forced to fail while an older physical row remains,
  every calls list/detail query excludes that row at the 14-day start cutoff;
  the matching closed correlation gap does not retire until a later read-only
  probe proves the row was deleted. A query that cannot enforce the cutoff
  reports the calls source unavailable, while a failed retirement probe only
  retains conservative coverage.
- A closed anomaly gap followed by 100 newer expiring rows remains unavailable
  before and after those rows expire; 100 newer non-expiring settlement rows
  permit retirement and preserve the newest-100 projection.

### Compatibility with retained surfaces

- Restart Engine still replaces only the sidecar.
- Rebuild/Repair still take the operation lease, prove the writer has no
  in-flight provider RPC before launching a child, keep intake closed for the
  operation, and resume only an admitted fresh generation.
- Clear still resumes from `clear-intent.json` and still recreates an
  empty provider root.
- A readable Clear intent replays, but an absent intent plus
  `memory_meta.clear_in_progress=1` remains the existing failed
  `orphaned-fence` projection and blocks migration, sidecar, and writer until an
  explicit operator Clear writes new authority.
- Clear's identity reset atomically removes provenance, anomalies, correlation
  guards, and observation gaps and nulls `last_success_at` before releasing its
  fence. A pre-Clear `failure_log()` row cannot appear under the new epoch,
  including on idempotent replay after a crash.
- Search, profile, list, remember admission, and agent-owner read paths
  keep their `origin/dev` contracts.
- Agent-provenance capture writes retain the current user-principal
  `ProviderSessionRef` and provider-session digest; no `-agent` write owner is
  introduced by this cut.
- Processing Record authorizes and correlates historical migrated calls and
  new add/flush calls through bounded provenance and observation-gap metadata,
  including rejected calls, linked memcell membership, unique-scope unlinked
  list/detail, restart-safe source unavailability, count-prune coverage,
  whole-request-id-group retention, and ambiguous-request-id tests.
- Processing Record reads recent durable failures from bounded
  `memory_processing_anomaly`, preserving its current newest-50 field shape,
  stable migrated ids, queue-backed 90-day expiry, settlement-backed persistence,
  disabled-state visibility, and unavailable-on-read-or-gap behavior without
  consulting queue or settlement tables.

## Implementation sequence

1. Identity-and-observer v4 schema + migrator + fixture property tests. No
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
      provenance, correlation guards, source-flagged durable observation gaps,
      and source-expiring sanitized anomaly rows; migrate retained correlations
      and the current failure projection before dropping the tables.
- [ ] Replace session final-flush waits with a best-effort multi-scope session
      barrier over the bounded volatile pending-flush map.
- [ ] Replace claim quiescence with writer pause/quiesce around Rebuild/Repair.
- [ ] Stop writing processing-fault ACK / `manual_required` / settlements.
- [ ] Scrub the entire confined pin root before every v4 writer start and reap
      stale pin completions plus every current/stale terminal release under the
      process-wide admission bound.
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
5. Session-boundary flush is a barrier enqueue, not a wait. A full admission
   queue can reject it. A crash/restart, disable, graceful shutdown,
   runtime-affecting configuration replacement, or Clear/Reset generation
   invalidation drops volatile due candidates; when the provider buffer survives
   the transition, consecutive conversations may share one EverOS accumulation
   boundary. A 257th distinct pending session is skipped before EverOS. Durable
   guards keep later correlation unavailable rather than silently omitting older
   message ids; successful Clear/Reset instead replaces the provider root and
   observer identity together.
6. No JSON identity file is introduced. Identity stays in the existing
   SQLite file so Clear's `reset_for_clear(target_epoch=...)` and
   Factory Reset keep one authority.
7. Pin-without-outbox is temporary debt. A later attachments PR may
   replace pins with ephemeral source leases; it is not required here.
