# Activity read batching

## Contract

Activity summary and detail retain their existing window, grouping, stable tool-step identities,
call-instance result boundaries, retry evidence, citations, and scoped media URLs. Legacy and
unknown-capability traces remain readable. New results, turn termination and media revocation
must appear on the next read, even on the same connection: caches live only for one projection.

The canonical tool projection currently issues two SQL reads per retained tool-calling response,
plus one media lookup per image. Batch these reads without replacing their bounded indexed
seeks with a scan of a Session's history. Parse retained canonical JSON once per projection and
reuse formatter instances only within that projection. No schema or migration changes.

The accepted-Delivery lookup must seek the retained message IDs, not all accepted Deliveries.
SQLite 3.45.1 still selected `recovery_owner(state=?)` for the actual 500-ID IN lookup after
bounded ANALYZE. Pin this read to `ix_message_deliveries_message`, installed by migration 0067
and retained by the supported schema-compatibility contract. The fixed SQL binds every ID;
no optional statistics job or new migration is required.

## Validation

- Existing Activity/projection tests, including reused call ids, opaque response barriers,
  fork isolation, retained windows, native hosted cards and summary/detail consistency.
- Query-count regression across multiple batches, per-response result limits, no stale cache
  after writes, scoped/revoked/missing media tests and EXPLAIN checks of the batched seeks.
- Synthetic large database generated and measured only in held local Incus, shared with
  planner-statistics and FTS investigations. Compare unchanged baseline and changed summary/detail
  on the same fixture without planner statistics as primary acceptance; record experimental
  post-statistics measurements separately. Record wall time and SQL counts, not owner data.
- Focused pytest and changed-file Ruff; exact-head Codex and required PR CI.

## Progress

- Root cause confirmed in `storage/tool_activity_service.py`: `response_steps` loops over
  `response_results` (next response + result interval) and `_media_urls` (one read per image).
- Search indexing is independent. Planner-statistics work was deferred after the actual
  500-ID lookup retained the unsuitable state-keyed plan; this PR no longer depends on it.
- Focused Activity and projection coverage: 98 passing tests, including full-window delivery
  EXPLAIN checks with and without ANALYZE. A 129-response fixture needs eight projection
  SELECTs, versus two per response plus the window/Turn reads before batching.
- Review caught the same nonunique-index trap on batched media: the Session predicate
  could select its historical media index. Fetch only by unique token and retain the
  existing Session/scope/revocation validation before attaching a URL. A 65-token,
  two-batch no-statistics EXPLAIN regression pins both reads to the token primary key.
- Final no-statistics acceptance on a 922,296,320-byte synthetic database (55,699 messages,
  452 Sessions, 7,302 Deliveries; SQLite 3.45.1, local Incus) used the full 500-message/
  2,000-trace Activity window, including 300 canonical tool-calling responses.
  - Summary: 626 to 17 SQL statements; median 149.696 ms to 99.126 ms.
  - Detail: 626 to 17 SQL statements; median 141.268 ms to 89.174 ms.
  - Summary/detail payload hashes match the unchanged baseline for every sample.
  - Actual delivery EXPLAIN changes from `recovery_owner(state=?)` to
    `ix_message_deliveries_message(message_id=?)`, with no statistics present.
  - Three baseline and five final sequential samples; OS page cache uncontrolled,
    no synthetic contention. Source-file hashes match the tested commit. Timing
    results describe this fixture, not the original audit's production latency.
- A prior planner-statistics experiment used the same synthetic database; its small
  statistics/evidence export was retained before removing statistics and reopening
  connections for final acceptance. Application counts, integrity and result hashes
  were verified unchanged. No owner database, full database copy or 6 GB load was used.
