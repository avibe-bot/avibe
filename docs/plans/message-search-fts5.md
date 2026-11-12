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

## Representation and rollout

Migration `20261112_0084` adds three initially empty ordinary tables and three
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
ids preserve complete search results while the worker retries. The index is
additive derived state, not a replacement for Messages. Corruption of an already
ready index is not silently reclassified as a module-capability failure.

## Acceptance evidence

Primary owner coverage is `tests/test_message_search_index.py`:

- real forward migration is empty/additive and preserves Message data and epoch;
  explicit isolated reverse-DDL removes only search artifacts;
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
- mutations to the trigger proof's SQL, source fields or target shape fail the
  additive classifier.

Existing Message search, controller maintenance, migration policy, no-copy and
compatibility-epoch suites are also run. Exact-head PR review/CI and a serialized
local Incus synthetic large-database benchmark remain required before readiness.
The benchmark must compare identical results before/after planner optimization
and indexing, migration/backfill duration, database/WAL growth and normal writer
latency. No live user database copy or owner-machine load test is authorized.

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
