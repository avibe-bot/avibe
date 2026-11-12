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

## Validation

- Existing Activity/projection tests, including reused call ids, opaque response barriers,
  fork isolation, retained windows, native hosted cards and summary/detail consistency.
- Query-count regression across multiple batches, per-response result limits, no stale cache
  after writes, scoped/revoked/missing media tests and EXPLAIN checks of the batched seeks.
- Synthetic large database generated and measured only in held local Incus, shared with
  P4 planner-statistics and P6 FTS lanes. Compare pre-change and changed summary/detail on the
  same fixture, after P4 lands. Record wall time and SQL counts, not owner data.
- Focused pytest and changed-file Ruff; exact-head Codex and required PR CI.

## Progress

- Root cause confirmed in `storage/tool_activity_service.py`: `response_steps` loops over
  `response_results` (next response + result interval) and `_media_urls` (one read per image).
- Search indexing is an independent change; large-fixture measurement is shared with the
  planner-statistics work. Final measurement waits for that prerequisite to land.
- Focused Activity and projection coverage: 95 passing tests. A 129-response fixture needs
  eight projection SELECTs, versus two per response plus the window/Turn reads before batching.
