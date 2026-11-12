# Resumable Message search acceleration

## Change contract

Message search remains literal substring search, not word search or an FTS query
language. It keeps the original SQLite `lower(content_text) LIKE lower(pattern)
ESCAPE '\'` predicate, snippets, transcript acceptance-time/id ordering, grouping,
limit, platform/type restrictions, project visibility/authorization restrictions,
hidden-session restrictions and archived-session opt-in. FTS only supplies a
superset of candidate Message ids; it never supplies returned content or access
control decisions.

The rollout must not synchronously scan or index historical Messages during
startup, put FTS tokenization on a Message writer, duplicate the stored message
text, or make older named-column writers depend on an optional SQLite module.
A crash must leave a resumable committed cursor, never a falsely complete index.
This is a background-indexing guarantee, not a blanket nonblocking-startup claim:
the existing migration preflight still requires a synchronous rollback snapshot
when importer completion cannot be proved. Fresh unpaired installations without
legacy deferred snapshots intentionally have no deferred-resource receipt, so a
later populated upgrade can take that conservative path and delay startup.
Neither the backup policy nor authorization provenance is weakened here.

## Representation and rollout

Migration `20261112_0086` (following the landed session-cache
revision `20261112_0085`) adds three initially empty ordinary tables and three
journal-only triggers, without changing the compatibility epoch:

- `message_search_state`: singleton activation row, backfill cursor and readiness.
- `message_search_dirty`: distinct changed Message ids, keyed by id.
- `message_search_documents`: stable integer document ids mapped uniquely to
  Message ids, plus an indexed flag for oversized-text fallback.

The insert, text/id-update and delete triggers enqueue ids only while the
activation row exists. Their only effect is an id-keyed `INSERT OR IGNORE` into
an independent table. They never call FTS, read Message history, or rewrite the
Message being accepted. Non-text updates (read receipts, delivery time, type,
platform and session changes) need no index rewrite: search obtains those values
from the current ledger.

The migration classifier admits this **exact complete trigger bundle**, only
alongside newly created, mechanically checked journal/state tables. It does not
permit arbitrary SQL, arbitrary triggers, virtual-table DDL, extra journal
constraints or an author-declared exception to compatibility proof.

After readiness, the existing joined storage-maintenance worker creates the
optional `message_search_fts` virtual table. It uses SQLite FTS5's case-sensitive
trigram tokenizer on SQLite-lowercased text, `detail=none` and
`contentless_delete=1`. The latter requires SQLite 3.43 or newer and stores no
second copy of the Message text. Integer document ids belong to the independent
mapping table, **not** `messages.rowid`, which can change during `VACUUM`.

The first batch activates the journal atomically with index initialization.
Backfill walks the Message primary key in ascending order, never OFFSET. It
advances independently of the dirty queue, so new writes cannot starve the
historical high-water mark. Each transaction processes at most 16 records;
indexing reads only a 65,537-character prefix and tokenizes at most 65,536
characters per record. Longer text is marked as oversized and searched with the
original predicate instead of silently truncating matches. All rows remain
searchable, including a term beyond that prefix.

Each batch starts `BEGIN IMMEDIATE`, reads the current Message text, replaces its
derived index entry, acknowledges its journal id and advances its cursor in the
same transaction. Writers cannot interleave between the read and acknowledgement.
A pre-commit crash repeats the batch; a post-commit crash resumes at the next key.
Cancellation rolls the batch back. Lock contention defers the batch after 50 ms,
and the connection's original busy timeout is restored before returning it to
its pool.

Maintenance yields between transactions. A pass has a five-second scheduling
budget (an in-flight bounded batch completes or rolls back before returning),
followed by a one-second pause while work remains. Idle checks run once per
minute. Retention/backup maintenance keeps its existing hourly check and separate
daily policy; disabling trace retention does not disable search indexing. The
worker uses the existing cancellation/join lifecycle, not a detached thread.

## Reads during and after migration

Until historical backfill is complete, the original query remains authoritative
for the entire history. There is no partial-results mode or search downtime.

Once ready, candidates are the SQL union of:

1. FTS hits for a bounded selection of quoted three-character tokens;
2. dirty Message ids not yet incorporated into FTS;
3. oversized Message ids that deliberately use the original predicate.

A `CROSS JOIN` fence drives id-keyed Message lookups from this candidate set.
Without that fence SQLite may still choose the old platform/type scan and probe
candidates per historical row. A query-plan test protects the actual lookup,
not merely the existence of the FTS table. Sorting remains over candidates that
pass all original filters, and LIMIT applies **after** those filters.

Readiness and the final query share a real SQLite read snapshot through a nested
savepoint, preserving a caller's existing transaction. This matters when another
runtime deactivates journaling: old readiness must not be combined with newer
unjournaled Message writes. Corruption or failure of the derived candidate path
logs only the error class and numeric SQLite code (never query text, Message
content, SQL parameters or exception text) and retries the original query once
with the same filters. A failure of the authoritative query still propagates.

Only ASCII upper/lower equivalence is applied in Python, matching SQLite's
built-in `lower`; no Unicode folding, accent removal or stemming is introduced.
Tokens are individually quoted/escaped and bound as a parameter. Punctuation,
FTS operator words, percent signs, underscores and backslashes remain literal.
Queries shorter than three characters and queries containing NUL keep the
original path. Common terms and oversized-message populations can still require
substantial work; this is not a constant-time or relevance-ranked search API.

## Unsupported runtimes, binary rollback and failure

Startup installs no FTS schema and does no backfill. If the running SQLite is
older than 3.43 or lacks FTS5/trigram, search stays on the original path. A worker
that cannot index removes the activation row and drains an existing dirty queue
in bounded batches. Subsequent writes do not accumulate an unused journal. The
controller warns on entering that state and retries on its idle maintenance
cadence. A later capable runtime starts historical backfill again before marking
the index ready; it cannot trust a cache that missed writes while deactivated.

Supported same-epoch binary rollback keeps the newer schema and triggers, as in
the SQLite compatibility contract. Older readers use their original query; older
writers keep the active journal current without knowing about FTS. Returning to
the new binary drains it normally. No automatic Alembic downgrade or restamping
is part of product rollback. The explicit reverse-DDL fixture drops the triggers
before their target tables and leaves Messages untouched.

Other indexing errors are logged, not treated as a successful rebuild. The
original query continues while backfill is incomplete; after readiness, dirty
ids preserve completeness during ordinary lag, and a failed index read falls
back to the original query. The index is additive derived state, not a
replacement for Messages. Corruption is not silently reclassified as a
module-capability failure or automatically repaired by deleting database pages.
The fault remains visible in diagnostics even when the ledger search succeeds.

## Acceptance evidence

Primary owner coverage is `tests/test_message_search_index.py`:

- real forward migration is empty/additive and preserves Message data and epoch;
  head-shaped unversioned model schemas replay safely, and explicit isolated
  reverse-DDL removes only search artifacts;
- partial backfill, insertion behind the cursor, text update, deletion, reopened
  connections and `VACUUM` preserve result identity;
- actual subprocess termination before/after batch commit, cancellation and
  write-lock contention preserve progress and journal acknowledgement semantics;
- accelerated and original results agree for escaped literals, CJK, accents,
  whitespace, FTS operator strings, platform/type filters, access scopes, hidden
  sessions, project archive, session archive and post-filter limits;
- unsupported-capability failure and older-version fallback do not break writes,
  accumulate an unused journal or reuse stale readiness on recovery;
- oversized text keeps suffix matches and transitions back into the index when
  shortened; the query plan reaches Messages through their primary key;
- concurrent index deactivation cannot mix readiness and ledger snapshots;
  caller rollback/read-only connections survive the savepoint; corrupt derived
  index data falls back without widening access;
- controller checks retry pending indexing without accelerating hourly retention,
  and mutations to the trigger proof's SQL, source fields or target shape fail the
  additive classifier.

Existing Message search, controller maintenance, migration policy, no-copy and
compatibility-epoch suites are also run. The serialized local Incus acceptance
below is complete on the identified source overlay; exact-head PR review/CI remain
required for delivery.
The shared fixture is capped at approximately 1.5 GB **total footprint**, including
the database, index and WAL, with a pristine database target at or below 1 GB.
Choose its final scale from a tiny sizing run and record actual file sizes; this
does **not** empirically validate a 6 GB database. The contentless index has no
aggregate disk-size cap: the per-Message text cap does not bound total index
expansion. Sampling, headroom and stopping before the footprint limit are part
of the measurement procedure, not a runtime storage guarantee.

The measured single-database progression initialized the then-P6 `0088` schema
through the normal initializer **before** synthetic fill. Old baseline source
then uses the same-epoch compatible-open path with FTS absent and journaling
inactive, followed by the experimental P4 planner-statistics pass and P5
comparison. P4 is held out of the release and is **not** a P6 landing prerequisite;
P6 has no code dependency on it. Primary P6 acceptance must compare baseline and
indexed search in a verified **no-statistics** planner state, because P4's optimize
hook does not ship. A post-P4/with-statistics result is secondary evidence only.
After P5 hands off, the approved final transition saves planner-statistics
metadata and immutable P4/P5 result hashes, then removes planner statistics from
this shared synthetic database without a full copy. Reopen every measurement
connection and verify no-statistics query plans before P6 baseline, backfill and
indexed parity. Record that transition and the exact source/schema/planner state;
there are no further shared-fixture consumers after P6. No statistics removal is
a product migration or an operation on a live/user database.
Preserve immutable result ids/order/hashes from each phase and the original
`0083` smoke evidence; do not fabricate importer receipts or bypass backups.
The original smoke's P6-only `0084` stamp predates the unrelated landed `0084`
runtime-checkpoint revision; it is evidence only and must not be opened or
migrated under the corrected graph. Export the corrected schema for the final
fixture. For final delivery, the orchestrator reassigned P6 to `0086` after the
landed `0085`; Vault and catalog now follow it as `0087` and `0088`. The measured
fixture retains its historical P6-only `0088` stamp and is not restamped or opened
under the renumbered graph. Schema DDL and index/search implementation are
unchanged; fresh/upgrade/replay tests cover the final `0086` graph.

The original `0083` smoke was 68,079,616 bytes with no `sqlite_stat1`; pre-renumber
candidate `20f149d` preflight classified its then-P6 `0084` as additive but
returned `skip_snapshot=False`
because importer receipts were incomplete. Migration and backfill were stopped
before changing that smoke. The revised fixture measures search parity, query
plans, background backfill duration, database/WAL growth, peak disk use and bounded
synthetic SQL writer contention. **Populated-upgrade and no-copy migration walltime are not
empirically measured** by this progression. Migration safety, replay and crash
coverage remain separate focused tests; they do not establish large-database
startup timing. No linear extrapolation from the smaller fixture is a measured
6 GB result. No live user database copy or owner-machine load test is authorized.

## Measured no-statistics acceptance (2026-11-12)

The single synthetic fixture contains 55,699 Messages, 452 Sessions, 7,302
Deliveries, 32,174 context events and 2,000 trace events (scale 0.14). Its pristine
database was 922,292,224 bytes. Schema and measured storage/core source came from
`0cc69728657d38a02ecd054d93964ee8c479dc0a`, over baseline
`4f5538d196288d278d5c7e36fdaf682b578ed72b`; this is a frozen source-overlay
benchmark, not a packaged-release or end-to-end service test. The measured
search/index code is unchanged by subsequent documentation and migration-identity
changes; final shipping revision is `0086`, not the measured fixture's historical
P6-only `0088`. No empirical migration claim relies on that old stamp.

P4's experimental pass produced 65 `sqlite_stat1` rows. Before the final P5/P6
phase, the fixture owner saved those rows and the post-P4 report fingerprints,
removed the statistics and reopened connections under explicit authorization.
Application counts, `quick_check` and foreign-key checks survived. Both P6 read
reports show no `sqlite_stat1`; the backfill report shows no statistics tables.
These are **no-statistics primary results**, not results that rely on shipping P4.
No database copy, populated upgrade or migration snapshot was performed.

Three samples per query used Python 3.12.3 / SQLite 3.45.1 on Linux arm64. Medians
include the same SQL capture/count hooks; hashing and EXPLAIN are outside timers.
Only the first overall call starts with a fresh connection, later calls reuse it,
and the OS page cache is uncontrolled: these are not physical-disk-cold timings.

| Literal query / variant | Before index, ms | Indexed, ms |
| --- | ---: | ---: |
| `watch` | 65.501 | 6.233 |
| `raretoken` | 104.287 | 1.740 |
| `commonterm` | 113.051 | 98.799 |
| `量子` (two characters, legacy path) | 74.956 | 72.382 |
| `literal%value` | 92.553 | 4.193 |
| `under_score` | 79.538 | 4.028 |
| `back\slash` | 67.553 | 2.845 |
| `suffixneedleoutsidecap` | 86.079 | 0.968 |
| absent synthetic term | 89.615 | 0.739 |
| `watch`, scope/hidden-session/archive filters | 54.071 | 4.752 |

For every case, all six before/after full-payload fingerprints match, and the
saved grouped Session/Message ids match in order. They also match the original
old-source pristine baseline. Captured plans show FTS candidate-driven Message
primary-key lookups for eligible queries; the two-character query retains the
original platform/type scan. Common terms remain expensive, as expected.

The direct worker harness ran 3,508 `run_batch` calls with 10 ms interbatch yields,
then one final dirty drain. Its reported elapsed time was 102.437 seconds,
including writer shutdown and final checks; the maximum observed batch was
46.829 ms. Readiness ended at 1 with no dirty ids or probe rows left, and application
counts were unchanged. This does **not** measure the controller's five-second
pass/one-second retry cadence or startup walltime, and the observed maximum is
not a guaranteed latency ceiling. Batch bounds remain 16 rows / 65,536 indexed
characters per Message; oversized content keeps authoritative fallback coverage.

Writer contention evidence consists of raw `sqlite3` INSERT+DELETE+COMMIT probes
on a separate connection, **not the storage-service API**. Five consecutive
pre-backfill samples had median 1.444 ms and maximum 3.461 ms. During backfill,
399 successful probes with 250 ms spacing after completion had median 1.848 ms,
empirical p95 9.221 ms and maximum 41.125 ms, with no errors or two-second busy
timeouts. Each probe inserted and deleted its row within the same transaction;
remaining dirty ids were drained before result parity. These numbers measure
bounded SQL writer contention, not normal service-layer or persistent-message
acceptance latency.

The post-index read report records a 974,413,824-byte database. The backfill
harness sampled aggregate artifacts before/after batches, observing a maximum
988,996,228 bytes including its 7,271,832-byte WAL and other retained artifacts;
this is a sampled peak, not continuous peak monitoring or a final report-directory
size. Its stop threshold was 1,450,000,000 bytes, reserving 50 MB below the 1.5 GB
budget. Reported process peak RSS was 55,928 KiB. No size/timing extrapolation to
6 GB, full service startup measurement, or real older-SQLite binary validation is
claimed.

Evidence identities (raw results retained by the fixture owner):

- Fixture manifest SHA256: `35cd41222b72b40a0f2f331b961ef82e9164f5fae997716ac2c6bf7a8270351a`.
- `final-p6-nostat-before.json`: `9d661e703d58645318fb41d4e417f89c6b480f5c0048373b5abfa0fb73317d5d`.
- `final-p6-indexed-parity.json`: `9ded8323e7cb47079a64e108a3fd9661bbbcfce5fff100d54f86c415e3218dbc`.
- `final-p6-backfill-writer.json`: `5fddf8ffed845f5a7f10c92686911058ee8674cd2b441fbeacce14b6837106e0`.
- Its metadata-only correction: `4f55696988cef94e5a508a28eb4e0b8c9df4935b420fbc913056e49a06f8e09e`.

The correction names the ambiguous raw `sqlite_stat1_primary=true` field as
`no_stat_primary=true`, preserving the raw report and all measurements unchanged.
The physical observations above, rather than that original field name, establish
the planner state. Reported source hashes were independently checked against the
frozen commit; the backfill producer's SHA256 is
`9df14ed65c9f8116bd983ba3465488e54e708f33429745dde67c431231a8b33a`.

## Intentional limits and follow-ups

- Two-character substring search retains the legacy cost; changing that needs a
  different index representation, not changed query semantics.
- A long rollback to an older binary has no new worker to drain its active
  journal. It stores one id per changed Message (not per update); returning to
  the current runtime resumes draining it. The old binary itself is unchanged.
- Oversized and extremely common terms may still be slow. Measure before adding
  another index, arbitrary result cap, or text-retention policy.
- Historical retention, database compaction, Activity queries and planner
  optimization belong to separate changes. This work neither deletes Message
  history nor runs FTS `rebuild`, FTS `optimize`, SQLite `VACUUM` or full ANALYZE
  during startup.
