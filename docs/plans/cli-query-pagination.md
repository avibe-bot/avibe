# CLI Query Pagination and Read-Only SQL Plan

## Background

Agent-facing list commands currently return all matching records. `vibe runs list`
only exposes `--status`, while `vibe show list` only exposes `--visibility`.
As Agent Runs and Show Pages accumulate, these commands need bounded defaults,
clear next-page hints, and a reusable path for future list commands.

Agents also need a more flexible exploration path for ad hoc conditions that do
not deserve first-class CLI flags. That path should be read-only SQL, not a
write-capable database escape hatch.

## Goals

- Add a shared pagination contract for CLI list/query commands.
- Default list commands to 20 records and include a next-page hint when more
  records exist.
- Add high-value filters to `vibe runs list` and `vibe show list`.
- Add a guarded `vibe data query` read-only SQLite query command.
- Keep simple, stable CLI flags as the primary day-to-day interface; use SQL for
  exploratory and low-frequency conditions.

## Design

### Shared Pagination

Introduce a small reusable module with:

- `PageRequest(page=1, limit=20, max_limit=100)`
- `PageResult(items, page_request, has_more)`
- helpers to clamp/validate CLI input, slice `limit + 1`, and build JSON
  pagination payloads.

All list/query commands should return:

- `pagination.page`
- `pagination.limit`
- `pagination.returned`
- `pagination.has_more`
- `pagination.next_page`
- `pagination.next_command`

Text output should append a concise "more records" hint when `has_more` is true.

### Runs List

Extend `vibe runs list` with:

- `--page`, `--limit` (always bounded; no unpaginated bypass)
- `--status`
- `--type`
- `--agent`
- `--backend`
- `--session-id`
- `--definition-id`
- `--created-after`
- `--created-before`
- `--q`

SQLite-backed runs should filter in SQL. File-backed fallback can filter in
memory; it is compatibility-only.

### Show Page List

Extend `vibe show list` with:

- `--page`, `--limit` (always bounded; no unpaginated bypass)
- `--visibility`
- `--session-id`
- `--updated-after`
- `--updated-before`
- `--q`

`ShowPageStore.list()` should support these filters and pagination using the
same shared result type.

### Read-Only SQL

Add `vibe data query`:

```bash
vibe data query --sql "select id,status,created_at from agent_runs order by created_at desc"
vibe data query --sql-file query.sql --limit 50
```

Safety layers:

- open SQLite with `file:...?mode=ro`
- set `PRAGMA query_only = ON`
- use `sqlite3.Connection.set_authorizer()` to deny writes, schema changes,
  attaching databases, transactions, and write PRAGMAs
- execute one statement with `execute()`, never `executescript()`
- use progress handler limits to avoid runaway queries
- force pagination/default limit behavior on result output

The SQL command is Agent-facing and should emit JSON by default through the same
CLI payload style.

## Validation

- Unit tests for pagination helpers.
- Runs store tests for filtering and pagination.
- Show Page store/CLI tests for pagination hints and filters.
- SQL query tests for SELECT success and write/DDL/multi-statement rejection.
- CLI parser tests for new arguments.
- Targeted ruff and pytest checks.
