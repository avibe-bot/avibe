# CI Database Seeding

## Scope and Order

The owner approved removing measured repeated database work in this order:
chat discovery setup, Harness history seeding, then incidental preparation in
migration and scheduling tests. Start from master `1ee2e1973`.

The initial scope is `tests/test_chat_discovery.py` and this plan. Reuse the
existing exclusive, per-test `sqlite_db_factory` with an explicit template built
by the original `run_migrations` recipe, not the importer-initialized default
template. Leave legacy import tests on their original initialization path.

Next inspect and profile Harness fixture transactions, then migration and
scheduling preparation. Expand the scope only with a measured, bounded cause.
Do not change application code, dependencies, runners, timeouts, selection,
real migration/locking behavior, or fail-closed CI aggregation.

## Invariants

- Every ordinary test gets a private copy in its isolated temporary directory.
- The template is closed and checkpointed before copying. Schema, every initial
  table row, persisted pragmas and integrity match real initialization.
- Mutating one copy cannot change the template or another copy; copying cannot
  overwrite existing state or escape the test directory.
- Original behavior assertions remain intact. Large-history query tests retain
  their complete data volume, real query plans and row/decode bounds.
- Historical upgrades, downgrades, legacy imports and real lock faults remain
  real operations. Only preparation outside the property under test may change.

## Evidence

Latest master CI `33981278943` passed all 17 checks and all 400 Python files in
6m52s. Its critical Python shard took 6m44s; chat discovery took 31.79s.
Recent green samples vary, so no runner-cause or stable speedup is inferred.

Local unmodified chat discovery: 32 passed in 18.14s. Thirty direct migrations
initialize the same empty schema; 28 belong to ordinary behavior tests. The
remaining two prepare actual legacy-import cases and are deliberately unchanged.
Local unmodified Harness: 224 passed in 25.90s; streak history 3.33s, predecessor
history 0.64s. Linux CI is slower, so local timing is not a CI speedup claim.

## Scope Decisions

### Step Two Scope Decision

Extend scope to `tests/test_harness_failure_visibility.py` only. A profile of
the two query regressions recorded 7,701 `enqueue_run` calls costing 8.00s,
versus 0.027s for the streak decision reads (profiling adds overhead). Static
history setup can share a transaction without changing the queries being tested.
Use the existing production serializer, connection writer and deferred-event
transaction owner; do not duplicate upsert or backend-identity logic. Cover
exact persisted-row equivalence, including an overwritten run, and one commit
for a batch. Reads, query/decode budgets and history sizes stay unchanged.

Step one passed 44 chat/fixture tests in 7.29s, including full schema/row/pragma
equivalence and private-copy mutation checks. This is not a CI speedup result.

### Step Three Scope Decision

Extend scope to `tests/test_sqlite_state_migration.py` for read-only reference
values only. The full original suite passed 128 cases under profiling in 47.61s;
21 `_upgraded_db` calls took 7.41s, and three identical index-reference builds
took 1.09s. Seven schema-reference builds repeat three revisions. Cache those
reference values as immutable sets and the index reference as a read-only map,
within this test module only. Every database under test still starts on its
original path and performs its original real migration calls. Do not cache
tested upgrade results or alter the historical revision inventory.

Harness after transaction batching: 225 passed in 20.80s; streak query case
3.33s to 2.75s locally. The whole-file comparison also contains unrelated import
variance and is not wholly attributable to batching. CI must establish I/O gains.

The added reference contract originally compared raw whole-schema DDL from two
fresh builds and caught nondeterministic Alembic foreign-key declaration ordering
in `message_deliveries` and `media_objects`. Existing `_schema_fingerprint` already
models structural equivalence for template tests. The reference cache contract now
pins immutable single-build reuse; all original migration-result comparisons remain
unchanged, rather than redefining their equality or migration behavior.

Scheduling profile: all 489 cases passed in 63.48s, including the real lock fault
(10.95s) and refused-result notice (5.47s). Remaining migration calls include real
importer/Agent-store entrypoints, not only fixture preparation. Leave this file
unchanged: bypassing these paths or reducing SQLite's production lock timeout would
change coverage for an unproven benefit. No blanket template rollout is justified.

## Review and Delivery

Final local checks: chat 33 passed in 6.80s (observer wall 6.97s), Harness 225
passed, real migrations 129 passed in 28.22s, workflow/shard/fixture/lifecycle
contracts 119 passed. All 340 original test definitions retain their 1,829
original assertions by AST comparison. Ruff 0.4.9 and dependency check passed.
No test file selection changes. Only these three test modules and this plan
are in scope; shared fixtures and production code remain unchanged.

Before review, integrate master `b5972b68c` (#1891, #1893, #1895). Inspection
of changed Model Hub/prompt APIs and consuming tests, and the chat paging
consumer confirms no overlap with this scope. Retest relevant Python consumers
after integration; production UI and all installation gates run in CI.

Initial findings-bearing review heads: zero. Keep one durable PR/CI watch and
its cursor through all heads; require exact-head review, all 17 checks, every
matching lint run, no unresolved whole-PR threads, and CLEAN integration before
guarded merge. Verify the merged source's own CI before final delivery.
