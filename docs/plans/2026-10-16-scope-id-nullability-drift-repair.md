# Scope ID Nullability Drift Repair

## Background

Some long-lived SQLite databases were stamped past
`20260723_0032_session_visibility` without running it. Their Alembic revision is
current, but `messages.scope_id`, `agent_events.scope_id`, and
`media_objects.scope_id` are still `NOT NULL`, and
`ix_agent_sessions_visibility` may be absent.

The affected tables can be large and have inbound foreign keys. Rebuilding them
would copy all rows and can trigger `ON DELETE` behavior while foreign-key
enforcement is enabled.

## Goal

Add a replay-safe migration after the current head that:

- removes only the three stale `NOT NULL` constraints;
- restores the visibility index when it is missing;
- performs no table DDL on a correctly migrated database;
- preserves every row and inbound foreign-key value; and
- leaves downgrade as a no-op because tightening the columns again cannot
  represent scope-less Sessions safely.

## Solution

Use SQLite's in-place schema-edit procedure for the three column constraints:
preflight the rewritten `CREATE TABLE` SQL, update `sqlite_schema` under
`PRAGMA writable_schema`, increment `schema_version`, and verify SQLite reports
the repaired columns as nullable. This keeps each table's root page and every
row unchanged, so it is constant-space and avoids a full table copy.

Create `ix_agent_sessions_visibility` separately with idempotent index DDL.

## Validation

- Upgrade a drifted previous-head fixture with rows and inbound references.
- Materialize a scope-less Delivery after repair.
- Prove a correct previous-head database keeps identical schema SQL, root
  pages, and schema version.
- Replay the repair through the head-shaped unversioned stamp path.
- Run focused migration tests, Ruff, the migration release guard, and the
  exact-head PR review and CI gates.
