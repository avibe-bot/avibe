# SQLite upgrades without full-database downtime

Status: proposal; plan only, no implementation or release authorization.
Evidence date: 2026-11-08 (+08:00). Source baseline: `15c5a8533`.

## Decision

Ship **A (compatibility protocol), then B (optional index jobs), then C
(trace-only storage separation)**. Keep the near-term snapshot/readiness work
in [PR #3365](https://github.com/avibe-bot/avibe/pull/3365) independent; its
`docs/plans/migration-startup-performance.md` owns that implementation.

Example: an upgrade adding only a non-unique performance index should not copy
4.5 GB merely to let yesterday's binary reopen it. After A's bridge release,
it keeps the same compatibility epoch and needs no migration snapshot. B can
also move the index build after readiness, **only if its absence is safe and
its write-lock budget is acceptable**. A table rewrite still takes a fresh
verified rollback point and runs before readiness. C reduces the amount of
history sharing the core database, not SQLite's locking rules.

Hard invariants:

- Old **writers**, not just readers, remain correct within a compatibility epoch.
  Unsupported or ambiguous state is refused before repair, backup, or import.
- No acknowledged canonical message, context entry, or external effect is lost
  or replayed by deferred work or a trace-store outage.
- Readiness means required schema and correctness indexes exist; it does not
  falsely claim optional optimization has completed.
- No migration requires filling the owner's disk, deleting unexpired history,
  or silently invalidating a rollback copy.
- Retention defaults, messages retention, message bodies, Vault/avault custody,
  cryptography, and remote operations are out of scope.

## Evidence and limits

The reported `3.2.5rc18` upgrade used a 4.5 GB SQLite database on a 91%-used APFS
volume: the **whole backup stage** took about 111 s, revisions 0073–0075 about
31 s, and downtime about 2.5 min. The restart supervisor's ordinary 120 s wait
false-failed. A reported `3.2.4` database is 6.1 GB. These are field observations,
not timings reproduced by this plan; the 111 s cannot retrospectively be split
into copying, waiting, verification, and filesystem publication.

The reported table footprint is approximately 1.7 GB `agent_events`, 1.5 GB
`messages`, and 351 MB `agent_runs`. **Not all 1.7 GB is disposable trace data**:
`storage/agent_events_service.py` and `storage/agent_transcript.py` also store
canonical tool results/checkpoints (`visibility='context'`, `context_seq`) and
non-retained audit events there. Measure the eligible subset before predicting
C's benefit.

Current owners are `storage/migrations.py` (revision preflight, migration lock,
repair), `storage/backups.py` (fresh snapshot and lifecycle),
`storage/importer.py` (startup/readiness), and
`storage/db.py` (WAL, 5 s busy timeout, selective write retries).
PR #3365 preserves snapshot verification and lifecycle. Its 3.27 GB synthetic
APFS test measured clone creation in milliseconds but backup `quick_check` at
9.803 s (~3 s/GB); paired end-to-end samples did **not** demonstrate a material
total-backup speedup. Non-COW filesystems still copy; COW also consumes physical
space as subsequently modified pages diverge.

### Complete migration inventory

Inventory and classification below cover upgrade bodies, including helper SQL,
not filenames or downgrade bodies. Historical labels are evidence for choosing
future mechanisms, **not permission to skip old migrations or their backups**.

There are **82 revision modules**, not 75: historical suffixes collide, so the
following ledger uses full IDs. Every upgrade and upgrade-reachable helper was
read. Partition: **30 ordinary schema extensions + 20 non-unique-index-only +
2 new-key uniqueness changes + 28 semantic/constraint/rewrite + 1 derived
backfill + 1 no-op = 82**, with no omitted/duplicated IDs. Thus 50/82 are ordinary
schema/index candidates; the rough "mostly additive" hypothesis holds only as a
structural observation, not an old-runtime compatibility proof.

All files are under `storage/alembic/versions/<ID>_*.py`.

| Upgrade shape | Count | Full revision IDs |
| --- | ---: | --- |
| New tables, sometimes indexes | 16 | `20260501_0001`, `20260515_0002`, `20260523_0004`, `20260526_0006`, `20260530_0009`, `20260602_0013`, `20260604_0016`, `20260608_0020`, `20260621_0023`, `20260707_0029`, `20260725_0035`, `20260725_0036`, `20260725_0037`, `20260725_0038`, `20260907_0061`, `20261020_0071` |
| Nullable columns, sometimes indexes | 10 | `20260603_0014`, `20260604_0015`, `20260604_0017`, `20260610_0022`, `20260705_0028`, `20260726_0036`, `20260731_0043`, `20260812_0053`, `20260923_0062`, `20261013_0065` |
| NOT NULL columns with compatible server defaults, no explicit rewrite | 4 | `20260525_0005`, `20260601_0012`, `20260627_0025`, `20260724_0034` |
| Non-unique-index-only, including replacements | 20 | `20260606_0018`, `20260606_0019`, `20260622_0023`, `20260721_0031`, `20260723_0033`, `20260727_0038`, `20260728_0039`, `20260728_0040`, `20260728_0041`, `20260729_0042`, `20260808_0048`, `20260821_0059`, `20261012_0064`, `20261015_0066`, `20261016_0067`, `20261016_0068`, `20261018_0070`, `20261107_0073`, `20261107_0074`, `20261107_0075` |
| New nullable identities plus correctness-critical UNIQUE indexes | 2 | `20260811_0051`, `20261002_0063` |
| Derived new-table backfill (session digests) | 1 | `20261030_0072` |
| No-op graph merge | 1 | `20260804_0047` |

The remaining **28** have these concrete non-additive/conservative reasons:

| Revision | Upgrade work requiring conservative treatment |
| --- | --- |
| `20260522_0003` | Legacy table/column renames and message/session-policy backfills |
| `20260529_0007` | Backfill routing from JSON |
| `20260530_0008` | Retarget references/default and delete legacy Agent |
| `20260531_0009` | Defaulted type column **and** classify existing messages |
| `20260531_0010` | Nullable source column **and** provenance backfill |
| `20260601_0011` | Normalize anchors, move transcripts, dedupe Sessions, add uniqueness |
| `20260608_0021` | Detach references and delete tool-call messages |
| `20260626_0024` | Remove values from existing JSON |
| `20260703_0026` | Vault data conversion, table drops/rebuilds |
| `20260704_0027` | Folded-name uniqueness and rejection triggers, not a performance index |
| `20260716_0030` | Rewrite harness message author/type before replacing indexes |
| `20260723_0032` | Rebuild nullability; rewrite Session anchors/scope/visibility |
| `20260726_0035` | Add dispatch state and mark historical events accepted |
| `20260726_0037` | Reconcile Show message identity; drop dispatch state |
| `20260801_0044` | Delivery/turn tables plus message movement/deletion and FK/check rebuilds |
| `20260802_0045` | Rebuild message uniqueness and change delivery dedupe keys |
| `20260804_0046` | Canonicalize message timestamps before index replacement |
| `20260806_0047` | Rebuild Sessions to remove queue-hold columns/check |
| `20260809_0049` | Rewrite notify to vault messages plus indexes |
| `20260811_0050` | Rebuild delivery receipt check |
| `20260811_0052` | Pause orphan-owned Tasks and rewrite metadata/timestamps |
| `20260815_0054` | New fields plus nullability rebuild and scoped uniqueness |
| `20260817_0055` | Visibility/access-mode conversion, backfills/checks/rebuild |
| `20260819_0056` | Replay five upgrades, including 0054's rebuild |
| `20260819_0057` | Rebuild deliveries to repair JSON server default |
| `20260820_0058` | Copy audiences to replacement table, drop old table |
| `20260821_0060` | Rewrite event timestamps; fetch batching is not transaction chunking |
| `20261016_0069` | Direct writable_schema/nullability surgery plus index repair |

Important qualifications: 0051/0063 leave old keys NULL, so old omitted-column
writes can still work, but new writers require their uniqueness and replay
fixtures may already contain values; neither is a deferred-index candidate.
0072 preserves source history but scans Sessions/checkpoints to derive data, so
it is not a constant-size startup step. Existing `MIGRATION_SAFETY` declarations
are release-test attestations, **not** this compatibility protocol: 0069 says
additive yet performs schema surgery.

For B, the 20 index-only revisions above are the retrospective extraction set.
**0064 is excluded unchanged** by a hard `INDEXED BY` consumer; the other 19,
including 0073–0075, qualify only after bounded-fallback/lock-budget review.
0040 is superseded by 0041; a future job registry should coalesce superseded
optional definitions. Index portions inside mixed revisions (notably 0030,
0046, 0049) do not make the whole revision deferrable.

## A. Compatibility epoch, not exact-head equality

### Protocol and cut-over

Use a monotonic integer **minimum runtime epoch**, rather than comparing package
versions. "Reader version" is misleading: an older process also writes. A
small protocol-v1 record in core SQLite contains the schema-family identifier,
epoch, bridge identity, current migration state, and an append-only record of
applied exact revision identities/ancestry. Keep Alembic's revision graph and
stamp; the marker does not replace migration ordering.

Proposed release boundary: **3.3.0 is the bridge (R0), 3.3.1 is the earliest
backup-skipping release (R1)**. These are planning targets, not promised or
published releases; the release owner must pin their actual tags before
implementation ships. The capability boundary is protocol v1, never a guessed
semver threshold. Neither `3.2.4` nor `3.2.5rc18` can be retrofitted.

1. R0 installs protocol v1, epoch 1, under the existing migration lock with a
   normal fresh backup. It teaches **every** opener (controller, Web, CLI,
   store/bootstrap and readiness probe) the protocol but keeps conservative
   backups for its own upgrades. All pre-bridge revisions remain conservative.
2. R1 may omit the snapshot only when an already-valid bridge record exists and
   **every pending operation** is classified additive. Jumping straight from a
   pre-marker release still takes a backup. Fresh empty databases need none.
3. A bridge-or-newer binary opening a newer compatible database enters an
   explicit **compatible-open** path: validate the protocol/epoch, its own head
   in the recorded applied ancestry, required schema, and no incomplete breaking
   attempt. Do not ask old Alembic to resolve unknown future revisions; do not
   restamp, repair, import, replay old data migrations, or prune as part of that
   open. Ordinary runtime reads/writes then use the approved epoch contract.
4. Known forward revisions still use exact Alembic graph reachability. Missing,
   malformed or unsupported marker formats, unrelated lineage, symbolic/partial
   stamps, inconsistent marker/stamp, unsupported epochs, and explicit ancestor
   migration targets fail closed with #3342's executable/version/path diagnostic.
   An unknown stamp alone never authorizes compatible-open.

This deliberately extends [#3342's contract](sqlite-migration-preflight.md), not
removes its preflight. Recognizing compatible future state must occur **before**
all mutation paths, including `_stamp_existing_initial_schema`, managed-index
repair, importer data rewrites, and `metadata.create_all` conveniences. A
compatible-open result must propagate to these callers, not just Alembic.

### What additive must mean

Each new migration declares `compatibility` (`additive` or `breaking`), required
and resulting epoch, execution phase (`startup` or `optional_after_ready`), and
an old-runtime compatibility test/justification. Default/unknown is breaking.
Classification covers accompanying runtime semantics and importer/repair work,
not merely the DDL file. Mixed batches take one fresh snapshot before any work
if any step is breaking; unclassified legacy repair remains conservative.

An additive change may create an independent table, append a nullable/defaulted
column, or add a proven non-unique performance index. It must preserve:

- Existing column names/types/meaning, valid values, row identity, ordering and
  conflict behavior; old reads still interpret **newly written** rows correctly.
- Old INSERT/UPDATE/DELETE behavior, including omitted new fields, unknown JSON
  fields, defaults, and `INSERT OR REPLACE`. Positional inserts and tuple-shaped
  `SELECT *` consumers need compatibility tests; extra columns are not safe by
  definition.
- No new NOT NULL without a compatible default; no new uniqueness/check/FK
  restriction or trigger that an old writer can violate. A non-unique expression
  index is not automatically safe either: its functions, JSON parsing and
  collations must accept all previously valid writes and run on the oldest
  supported SQLite library.
- No rewrite/delete of existing payloads, table rebuild/drop/rename, changed enum
  semantics, or new feature requiring old binaries to preserve state they would
  overwrite. Such changes raise the epoch even when they need no DDL.

CI extends the existing migration-release guard rather than replacing it:

- Require literal metadata, immutable released history, a valid graph, monotonic
  epoch transitions and phase/dependency declarations.
- Restrict additive execution to a small reviewed operation API. Reject arbitrary
  SQL/data updates, triggers, uniqueness, table rebuild/drop/rename, and opaque
  Python mutation helpers in the additive path. Instrument real executions and
  compare schema plus existing-row projections before/after on legacy, dirty,
  replay and partially-applied fixtures; declarations alone cannot grant safety.
- Run each supported released runtime in the current epoch against the new DB:
  read **new-version data**, perform real old writes, then reopen with the new
  runtime and verify invariants. Include pending optional jobs and every startup
  entry point. Pin representative enum/JSON and default behavior, not just SQL
  success. Mutant cases (new constraint, changed meaning, dropping unknown fields)
  must make the guard fail.
- Require independent review of semantic compatibility. CI cannot prove arbitrary
  program meaning; if the guarantee cannot be demonstrated, choose breaking.

### Crash safety, downgrade, and retention

The migration runner owns explicit transactions: additive DDL, its revision
stamp and compatibility ancestry receipt commit together. Do not rely on the
Python driver's implicit DDL transaction behavior. Repair helpers outside that
transaction cannot ride the no-backup path. Inject crashes between each step;
reopening must see either the old state or a replay-safe committed prefix.

For breaking work, take a fresh verified snapshot **on every attempt**. Persist
the higher epoch and an incomplete-attempt fence before any incompatible change;
clear the fence only after successful validation. Failure leaves old runtimes
refused, not falsely compatible. New-protocol clients hold a shared schema-use
lease while using the database; epoch-changing migrations require its exclusive
side and refuse live mixed-version users. Reuse the migration lock for migration
serialization, but do not pretend that lock already excludes ordinary writers.
Pre-marker processes must be stopped during cut-over; arbitrary old external
SQLite clients cannot be made cooperative retroactively.

Within an epoch, rollback means selecting the older compatible binary with no
Alembic downgrade or restore; new columns/tables/indexes remain. Across epochs,
or to a pre-marker binary, rollback means an explicit verified restore with all
processes stopped. It may discard writes since the snapshot; never auto-restore,
lower the epoch, or restamp to make a binary open. Prefer forward repair.

Preserve [#1578's rollback-position window](https://github.com/avibe-bot/avibe/pull/1578):
positions remain exact revision sets, with first/last attempts and write sequence,
**not epoch groups**. Preserve [#2252's 72-hour expiry](sqlite-backup-expiry.md):
only real successful migration/import/readiness evidence authorizes age expiry;
failed, restored and unconfirmed attempts retain existing protections. A no-copy
upgrade creates no fictitious backup receipt and must not adopt a prior attempt.
Existing independently eligible copies may still expire on maintenance.

Keep the existing post-Alembic and post-import live validation boundaries even
on no-snapshot startup upgrades; decouple them from backup receipts rather than
silently dropping their checks. Same-head read-only startup remains fast. A
therefore removes snapshot copy/verification, **not every database-sized startup
check**; changing live validation is a separate decision.

## B. Ready first, optional indexes later

Only **non-unique, correctness-independent indexes** qualify initially. Required
tables/columns, constraints, data backfills and semantic rewrites remain before
readiness. Published revisions are not edited: legacy upgrades replay as before;
new migrations register optional jobs and stamp that registration atomically.
An unfinished job does not claim its index exists. A later correctness-dependent
migration must explicitly await/promote the prerequisite before running.

Use one controller-owned maintenance worker and the existing migration lock,
not a second Alembic runner. Persist job identity, expected index-definition hash,
state, attempt count, timestamps and last error in core SQLite. A job executes
on a dedicated worker connection, verifies `sqlite_schema` and query-plan
postconditions, then records success. Restart reconciles actual DDL with the
receipt (including crash-after-DDL); missing or mismatched definitions are not
success. Old compatible runtimes ignore unknown jobs without deleting or running
them. Startup repair, `ensure_background_indexes`, and readiness probes must all
respect the same optional-index registry or they recreate the outage.

**Missing must mean slower, never incorrect or SQL failure.** Audit `INDEXED BY`
and `ON CONFLICT`/uniqueness dependencies. Today `storage/agent_transcript.py`
explicitly names `ix_messages_turn_id`, `ix_agent_events_turn_sequence_id`, and
`ix_message_deliveries_turn`: those are not deferrable without changing their
callers. Do not allow an unindexed history scan onto the controller loop; either
prove a bounded existing-index fallback and run slow reads off-loop, gate the
particular optimization, or keep that index before readiness. Pin both missing
and installed plans in tests.

### Write-lock policy

SQLite has one writer per database. `CREATE INDEX` holds that writer for its
transaction; WAL lets readers continue but does not make the index build
concurrent with writers. It cannot be checkpointed into resumable chunks.
`busy_timeout=5000` means an unprotected writer can fail after 5 s. #3281 retries
only canonical transcript/activity/outcome transactions (up to about 15.15 s),
not every writer, and is **not** permission to occupy the writer for 31 s.

Decision: automatic after-ready work accepts a **short measured lock**, not an
unbounded build hidden behind a ready response.

- Start after a quiet interval (initial target: 30 s without active/queued turns),
  low disk pressure, and cheap lock admission (about 100 ms). This is a heuristic,
  not a promise that Web/CLI writers cannot arrive afterward.
- Target <=1 s total writer occupancy, including rollback/cancellation. Use a
  progress-handler deadline and stop on foreground demand where observable.
  Validate the actual cancellation/rollback tail on supported SQLite versions;
  if it cannot stay comfortably below the 5 s writer timeout, disable automatic
  builds of that size. No increase to global busy timeouts and no external-effect
  retries. Interrupted builds start over; they do not resume partway.
- Retry transient contention with backoff in later idle windows; after three
  interrupted attempts, leave `needs_maintenance` rather than repeatedly scanning
  the table. Disk-full/schema/integrity failures pause immediately with actionable
  diagnostics. Readiness stays valid only because the job is truly optional.
- A build exceeding that budget needs an explicit quiet maintenance window.
  Keep status/read-only service live, drain active work and quiesce **all** core,
  Web and CLI writers using the shared admission contract. New writes get an
  honest retryable maintenance response, never a success before durability.
  If complete quiescence is unavailable, use the ordinary pre-ready path.

This trades possible delayed optimization for predictable foreground behavior.
Idle detection alone cannot guarantee zero write disruption. The reported
0073–0075 total must be measured per index; "run in background" cannot erase
those write locks.

Expose `pending / running / complete / deferred / failed`, elapsed time, retry
schedule, reason and last error through status/doctor and structured logs.
SQLite provides no trustworthy percent-complete for `CREATE INDEX`; show phase
and elapsed time, not invented progress. Optional-job completion does not reset
backup expiry or become service readiness. Run any retained full integrity
verification off-loop after the job, without holding its write transaction.

## C. Move retention-class traces, not canonical history

### Storage boundary

Keep `messages`, `agent_runs`, session/delivery/turn state, canonical context,
audit events, and Vault-related tables in `vibe.sqlite`. Initially externalize
only `agent_events` rows with `event_type='tool_call'`, `visibility='trace'`, and
`context_seq IS NULL`. Unexpected context-bearing or other event classes stay
in core. This is narrower than moving the table and preserves the canonical
transcript's single-database sequence allocation and commit. Old message bodies
would add hot-path joins and retention semantics without evidence of benefit;
they are explicitly excluded.

Use one `state/traces.sqlite`, a separate engine/WAL/migration history and a
persistent store identity paired with core. Daily UTC partitions inside that
file permit dropping **wholly expired eligible partitions**; the cutoff day
still uses bounded row deletion. Preserve malformed timestamps in an undated
partition, retention opt-out, configured duration, and the current strict
`created_at < cutoff` rule. Partition catalog/query construction must also work
with disabled or very long retention beyond SQLite's compound-SELECT limits;
never omit old partitions to fit a query. Create this new database with incremental auto-vacuum
before its tables, then reclaim dropped-partition free pages in bounded idle
steps. One undivided second table would separate locks but still need row
cleanup/VACUUM; one file per day adds manifests, handles and restore complexity
unnecessarily for the first implementation.

Partition drop is a SQLite schema operation, not filesystem unlink or instantly
free space; budget/measure its writer hold on the trace file. Trace-only inserts
and retention no longer take the core writer. Canonical context still does;
shared CPU/disk pressure, WAL growth and any remaining core bookkeeping remain.

### Consumers and transactions

The current cross-table dependencies are narrower than "move agent_events":

| Owner / operation today | Split consequence |
| --- | --- |
| `storage/agent_activity_service.py`: reads messages, tool-call/silent-terminal events and durable Turns, then groups them in Python | Extend the existing composition to legacy core plus trace store; preserve ordering, tail limits and terminal grouping |
| `storage/agent_transcript.py`: message/event context-sequence allocation, fork/context reads, checkpoint plus `session_digests` in one transaction | Keep all of it in core; no new cross-file commit or context-read lag |
| `storage/cache_metrics.py`: UNIONs message facts and audit-event JSON | Remains core-only because audit events do not move |
| `storage/skill_observability.py`: inserts trace observations with daily aggregates; clears both together | Keep Skill event classes and aggregates in core in this first split |
| `storage/read_only_query.py`: raw logical `agent_events` and user-authored SQL joins | Preserve the logical table through trusted read-only ATTACH/views; transcript/turn/digest views stay core-only |
| `core/message_mirror.py`: tool trace insert and Session activity touch inside `engine.begin()`, publish after commit | Explicitly retain durable coordination where required through outbox/reconciliation; publication follows durable receipt, not speculative insertion |
| `core/chat_discovery.py`, `storage/sessions_service.py`, and `storage/models.py`: scope-history guards, explicit cleanup and scope CASCADE/session SET NULL FKs | Query both stores for guards; replace cross-file FK effects with core-authoritative intents/read filtering |
| `storage/agent_events_retention.py`: eligible row deletes plus state_meta lease/run receipts; optional whole-core VACUUM | Route policy across both locations during drain; keep policy owner, give each file separate reclamation/status |

The shipped activity path already composes reads rather than a SQL join of
externalized tool calls to messages. The hard same-transaction dependencies are
canonical context/digests, Skill aggregates, mirror activity and lifecycle
cleanup; moving the entire table would incorrectly cross those boundaries.
`core/services/session_fork.py` also reads `silent_terminal` for fork boundaries;
those non-tool-call rows remain core-resident.

Normal services use bounded application-level composition: authorize/resolve
scope/session in core, fetch indexed trace slices, merge by stable
`(created_at, id)` with deduplication while legacy and new stores coexist.
Do not replace a paginated query with "load all traces then join in Python".

For `vibe data query`'s existing cross-table SQL, allow one controlled read-only
`ATTACH` of `traces.sqlite` and connection-local TEMP compatibility views that
union core events with the trace partitions under the existing logical schema.
Keep authorizer/column restrictions and schema discovery intact; arbitrary
ATTACH or paths remain forbidden. Read transactions across WAL files are **not
a cross-file snapshot**: trace inspection may lag concurrent core changes, while
canonical transcript queries retain their single-core snapshot. Missing/failed
trace storage must be reported as unavailable/partial, not silently empty.

Do **not** use attached WAL writes to pretend a two-file commit is atomic.
Canonical transcript transactions remain entirely in core. Separate standalone
trace appends from ancillary core activity updates; where a current operation
requires both effects to be durable, use a minimal core outbox with stable event
IDs, idempotent trace application, then acknowledgement/removal. Do not replay
model/tool/network calls. Bound outbox growth and report backpressure/failure;
never treat failed trace persistence as success silently. Avoid routing every
trace append through core merely for convenience, which would forfeit much of
the lock-separation benefit. The current mirror path does touch core Session
activity (and can register hosted media), so it is **not** already a standalone
trace writer. If it needs a core outbox, measure its write amplification; C may
then primarily remove retention/compaction contention and long-lived payload
bloat rather than eliminate core writes per event. No zero-contention claim is
an acceptance criterion.

SQLite cannot enforce the current cross-store scope/session FKs. Core deletion,
rebind and reclaim must record idempotent trace reconciliation intents in their
own transaction. Readers apply core tombstones/rebinding while the worker
catches up, preventing stale authorization or deleted-scope visibility; partition
expiry and copy workers honor the same intents. Preserve scope CASCADE versus
session SET NULL behavior. This coordination is part of C, not a later cleanup.

### Online transition and low-space path

A C-enabled release raises the core epoch **before** routing any writes outside
core, and takes A's fresh verified rollback point. The transition manifest
records layout generation/store identities and `legacy / dual-read / split`
state. All writers use this routing contract; do not permit an old binary to
write into the abandoned core trace table.

1. Create the small trace store and its indexes, validate identity, then publish
   dual-read/new-trace-write routing under a brief exclusive cut-over. Canonical
   context never changes location. If setup fails, remain on legacy routing.
2. **Default at low free space: zero-copy drain-in-place.** Existing eligible
   rows remain readable in core and expire only under today's retention policy;
   new eligible rows enter the trace store. No 1.7 GB eager copy, no retention
   reduction, no whole-core VACUUM during startup. With default enabled 30-day
   retention, the old eligible cohort drains normally; opt-out/unparseable or
   excluded rows remain indefinitely until safely moved or separately handled.
3. With sufficient measured headroom, optionally copy by bounded keyset batches
   (stable IDs, not OFFSET), durably commit and verify destination contents, then
   delete only the matching source versions and advance a durable cursor. While
   both copies exist, core is authoritative; a changed source version is recopied,
   not hidden by the destination. Crash after copy yields duplicate IDs resolved
   by dual reads; crash before copy
   never deletes the source. Rebind/delete intents and destination tombstones
   prevent stale resurrection. Resume from receipts, not guessed row counts;
   coordinate retention so it cannot erase unverified copied content. Bound WAL,
   temporary space and time per batch; pause on resource pressure.
4. Finish only after per-batch content checks plus residual-source reconciliation,
   then retire the legacy eligible read path. Keep core context/audit rows and
   indexes. Core file compaction is optional, separately budgeted maintenance.

**Space constraint, not an implementation trick:** deleting core rows generally
adds freelist pages without shrinking its file. Repository connections do not
enable auto-vacuum; switching an existing NONE database to incremental mode
requires a rebuild. Thus even copy/delete in tiny batches can need the entire
remaining trace footprint in extra disk space. Never infer safety from "91%
used" alone: measure free bytes, page/freelist counts, expected trace bytes,
backup/COW divergence, WAL and temporary peaks plus a reserve. Pause copying
unless the *remaining* requirement fits, not merely the next batch.

The drain path avoids duplicating the old 1.7 GB but does not immediately return
1.7 GB to the filesystem, and new traces still need space. A fresh breaking
backup may itself not fit on a non-COW volume; then defer cut-over or use an
explicitly approved backup destination with capacity. There is no honest
portable promise of full rollback, immediate physical shrink and zero extra
space. Do not attempt hole-punching or change journaling safety to achieve it.

### Backup, restore, downgrade

Back up/verify/compact each file independently when only that file changes.
Core-only additive upgrades should not scan trace payloads. Trace schema changes
have their own compatibility epoch and lifecycle; core records the minimum
supported trace layout. Whole-instance backup/restore uses a versioned file-set
manifest with identities, epochs, migration/copy cursors and outbox boundaries.
Quiesce writers for a coherent checkpoint or use a defined replay fence; two
sequential WAL backups do not form an atomic snapshot by themselves.

Extend #1578/#2252 at **file-set** ownership before enabling C: one restore point
is not partially expired, and existing single-file manifests stay readable by
the appropriate restoring version. Do not let an older pruner misunderstand a
new file set. An older same-epoch C-aware binary can use the split layout; a
pre-C binary refuses it. Pre-C downgrade restores the complete pre-cut-over
snapshot (losing later writes only with explicit consent), or uses an explicit
merge-back/export tool with enough space and validation; no automatic reverse
migration. Deleting the sidecar is never a downgrade procedure.

## Delivery sequence, estimates, and acceptance

Estimates are engineering effort for one owner including tests/review, not wall
clock promises or release dates. Full implementation is **about 5–8 weeks**,
plus bridge adoption and optional legacy-drain time; stop after A/B if C's
measured eligible footprint is too small to justify it.

| Stage | Dependency | Effort | Exit evidence |
| --- | --- | --- | --- |
| Existing #3365 snapshot/readiness lane | Independent, no duplication here | Owned elsewhere | Correct fallback/verification; truthful migration-aware startup |
| A1 protocol/bridge + classifier/CI + all-openers audit | #3342 contract; coordinate #3365 | 5–8 days | Real old/new writer matrix and crash prefixes; conservative R0 upgrade |
| A2 no-copy additive path + downgrade/retention integration | A1 shipped, pinned bridge tag | 3–5 days | No snapshot only for proven additive batch; breaking attempts retain fresh rollback |
| B optional index registry/worker/admission/status | A1 contract; preferably after A2 | 4–7 days | Missing-index consumers safe; foreground writer and loop budgets met |
| C traces-only store, consumers and reconciliation | A shipped; B worker reusable, not mandatory | 10–15 days | No context change, crash-safe split/copy/restore, low-space deferral |
| C rollout/partition reclaim and full-size qualification | C implementation | 3–5 days | Actual footprint and lock benefit, not just smaller table counts |

Implementation PRs must update the owning contracts and tests, not edit released
migration history or stack on an unmerged contract. Public English/Chinese
upgrade documentation is updated with the implementation, not promises from
this plan. No live database/service modification is authorized here.

### Measurement plan (isolated, owner-sized synthetic state)

Use both 4.5 GB and 6.1 GB datasets with representative 1.7 GB event and 1.5 GB
message distributions, realistic index cardinality/JSON sizes, and separate
trace/context/audit cohorts. Vary cold/warm cache, fresh/freelist-heavy layouts,
APFS clone, Linux reflink, and a non-COW copy path. Use bounded test volumes to
simulate low headroom—never fill the owner's volume or copy personal content.
Record SQLite/Python versions, disk/free bytes and allocated versus logical size.

| Area | Measure / falsify |
| --- | --- |
| Baseline and #3365 | Service-stop to true readiness; migration-lock wait, snapshot checkpoint/copy/clone/quick_check/fsync, each revision, both live validations, controller startup; supervisor result. Do not relabel the historical 111 s as pure copying. |
| A | Same-head, additive, mixed/breaking, missing marker and incompatible epoch; snapshot count/bytes and exact refusal side effects; all supported old writes against new rows; marker/stamp consistency after every injected crash. Compare readiness with and without snapshot while retaining live checks. |
| B | Per-index build/lock/cancel/rollback duration, p50/p95/p99 core write latency, busy errors/exhaustion, event-loop stalls, incoming work during "idle", and missing/installed query plans. A 6 s and 20 s contender proves #3281 is not universal protection. Abort automatic mode if <=1 s occupancy cannot be upheld; no lost/duplicated acknowledged writes. |
| C | Eligible bytes first; per-file write occupancy, trace throughput, canonical write latency, WAL/temporary/outbox/allocated-space peaks, copy and drain cursor progress. Exercise full-disk failures before/after each commit, crash between copy/delete, delete/rebind during copy, missing sidecar, read pagination and FK-equivalent behavior. Verify actual file shrink separately from freed pages. |
| Recovery/retention | Upgrade/downgrade matrix across pre-marker/R0/R1/pre-C/C; restore after new writes, interrupted breaking migration, same-epoch old startup with pending jobs, first/last-position retention, exact 72-hour eligibility and whole-file-set expiry. Partition cutoff, opt-out and malformed timestamps must preserve current policy. |

Report median and tail across repeated paired runs, exact resulting rows and
receipts, and peak physical space. A/B's win is reduced *unavailable time*, not
merely a faster health response while writers are still blocked. C must show
less core contention without weakening canonical durability or authorization.

## SQLite references

Primary documentation checked for this proposal:

- [Transactions](https://www.sqlite.org/lang_transaction.html): one simultaneous
  writer; CREATE is a write transaction; BUSY and rollback behavior.
- [ATTACH](https://www.sqlite.org/lang_attach.html): WAL transactions are atomic
  per file, not across attached files on a host crash.
- [Auto-vacuum](https://www.sqlite.org/pragma.html#pragma_auto_vacuum): NONE does
  not shrink on deletion; existing-file conversion requires VACUUM.
- [VACUUM](https://www.sqlite.org/lang_vacuum.html): rebuild/temporary-space cost;
  VACUUM INTO is a new copy, not in-place online shrink.
