# Skill catalog reference readers

## Rollout and change contract

This is the **reader-first 3.2.6 stage**, not write activation. Revision
`20261112_0087` adds one empty, independent `skill_catalogs` table. It does not
scan, rewrite, backfill, or delete existing events. The compatibility epoch and
supported reader epochs are unchanged. Every production observation still writes
its complete inline `entries` array, byte-for-byte as before. This stage alone
therefore provides **no database growth reduction**.

The subsequent coordinated 3.2.7 representation change owns write suppression and
its breaking epoch boundary, after all participating readers are released. A
binary must not advertise that later epoch while it only understands one of the
representations. Follow [SQLite compatibility](../maintenance/sqlite-compatibility.md):
select an older supported same-epoch binary without restamping or Alembic
downgrade; crossing epochs requires an explicit verified restore with writers
stopped and loses writes since that snapshot. This PR performs no release or
live-state operation.

## Representation

- `skill_catalogs.digest` is the primary key; `entries_json` holds an entries
  array. Its identity is SHA-256 of UTF-8 JSON serialized with
  `ensure_ascii=False`, `sort_keys=True`, and `separators=(",", ":")`.
- This storage identity is **not** the observation's `catalog_digest`, which
  remains an unchanged producer field. Derive storage identity from actual
  entries, never trust a submitted digest to select another listing. Array
  order, positions, source kinds, names, descriptor hashes and skill keys remain
  part of the identity.
- A future compact `skill.catalog_result` trace replaces only `entries` with
  `entries_ref: <storage digest>`. Event identity, timestamps, attribution,
  metadata, entry point, page, outcome and `has_more` remain on each event.
- Inline entries take precedence, even when an extra reference is present.
  Non-catalog/non-trace events are never interpreted as catalog references.
  Missing snapshots, invalid reference syntax, invalid JSON, non-array snapshots
  and digest mismatches fail visibly rather than producing an empty catalog.
- Hydration removes the storage-only `entries_ref` and restores `entries` in the
  returned content. It never updates stored rows. The catalog lookup is by
  primary key. Session reads use a savepoint to keep events and catalogs in one
  SQLite snapshot even under sqlite3's legacy SELECT transaction behavior;
  releasing it does not commit caller-owned writes.

## Readers and permissions

`storage.agent_events_service` owns event payload hydration, including ordered
and bounded session reads. The Skill recorder's duplicate-receipt comparison
also hydrates an existing reference: retrying the original inline observation
returns `duplicate`, does not increment daily counters, and does not replace the
stored representation. Changed submitted content still conflicts. New writes
remain inline; there is no deduplication writer or runtime switch in this stage.

`vibe data query` exposes **`skill_events`**, a connection-local, explicitly named
hydrated projection of the two Skill trace types. Its columns match
`agent_events`, including reconstructed `content_json`; JSON extraction and
`json_each(..., '$.entries')` work for both representations. The physical
`agent_events` and `skill_catalogs` tables remain raw, as with existing diagnostic
views; there is no TEMP shadow of a physical table and no SQL text rewriting.
Queries that need logical catalog entries should use `skill_events`.

Global Skill diagnostics retain the existing owner/Member floor. The authorizer
applies the same restriction to the new catalog table, including direct reads,
CTEs and joins. Editors, viewers and unauthenticated remote principals cannot
use the table or projection to bypass the original event restriction. Local
query behavior is unchanged.

## Retention and forks

No new cleanup or lifecycle policy is activated in this reader stage. Production
creates no catalog rows. Synthetic compact fixtures show a shared listing remains
readable after another event expires or its Session is deleted. Catalog snapshots
are independent of the first event/Session; deleting one owner must never remove
a listing still referenced elsewhere.

Skill observations are trace rows without `context_seq`. Transcript forks only
inherit rows with `context_seq`, so catalogs are not inherited model context.
Separate events in a parent or fork Session may nevertheless share the same
catalog and must be treated as independent references.

**Write-activation prerequisite:** the coordinated follow-up must atomically
persist catalog/event/reference state with daily counters; maintain indexed
reference ownership; integrate bounded retention, explicit Skill clear, Session
and scope deletion; and reclaim a listing only after its last reference is gone.
It must retain the existing 90-day raw and 365-day daily policies. This stage
intentionally retains any fixture/imported catalog rows rather than introducing
premature garbage collection. No full historical rewrite or VACUUM is required.

## Verification

`tests/test_skill_catalogs.py` owns the reader contract:

- populated old-schema upgrade leaves existing event bytes intact and epoch 2;
  production writes before/after are identical and leave the new table empty;
- legacy, referenced, empty and shared catalogs round-trip through event payloads
  and SQL JSON functions without read-side mutation;
- duplicate receipts preserve raw reference bytes and daily counts;
- missing/corrupt references fail visibly in both readers;
- inline precedence, unrelated event isolation and existing authorization floor;
- shared snapshots survive another event's retention and Session deletion;
- concurrent deletion of an event and its last catalog cannot tear an in-flight
  session read (the regression fails with a missing snapshot without the fix);
- query plans use event/session and catalog primary-key searches, with no
  history-wide scan or temporary sort for bounded session reads.

Existing Skill observability, event retention, query, compatibility and no-copy
coverage protects the surrounding contracts. The historical bridge fixture pins
its historical schema requirements as well as its migration graph, including its
crash subprocess, so later independent tables do not falsely become old-reader
requirements.

Remaining deployment check: in the lead's serialized local Incus slot, migrate a
populated **test-owned** instance, verify ordinary Skill list/load plus the
hydrated SQL fixture, and confirm production observations still store inline
entries. No Incus operation is performed while that slot is frozen. Real released
predecessor-wheel compatibility remains a CI/release gate, not an inference from
local source fixtures.
