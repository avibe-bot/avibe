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

`pagination.next_command` is the only continuation: no top-level prose repeats
it. A page whose command cannot be replayed (SQL piped on stdin, which has no
`next_command`) adds one `message` telling the caller to add `--page`.

### Task and Watch Lists

`vibe task list` and `vibe watch list` take `--q` with the same substring
semantics as `vibe runs list --q`, searching the store's definition text fields
(the Workbench search), and carry it into `next_command`. Enabled definitions
come first, each group ordered by most recent activity.

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
  attaching databases, transactions, and every PRAGMA except the schema-reading
  `table_list`, `table_info`, `table_xinfo`, `index_list`, `index_info`, and
  `foreign_key_list` (statement or `pragma_*` table-valued form), so Agents can
  discover the schema with the query they already reach for
- execute one statement with `execute()`, never `executescript()`
- use progress handler limits to avoid runaway queries
- force pagination/default limit behavior on result output
- on `no such table` / `no such column`, return a `hint` with the closest names
  and the table list or the referenced tables' columns; on a refused PRAGMA,
  name the allowed ones. The hint is advisory: if reading the schema fails, the
  query's own error is returned unchanged
- the connection keeps Python's default 5 s busy timeout, the same bound as
  `storage/db.py`; WAL readers do not wait on writers

The SQL command is Agent-facing and should emit JSON by default through the same
CLI payload style.

## Validation

- Unit tests for pagination helpers.
- Runs store tests for filtering and pagination.
- Show Page store/CLI tests for pagination hints and filters.
- SQL query tests for SELECT success and write/DDL/multi-statement rejection.
- CLI parser tests for new arguments.
- Targeted ruff and pytest checks.
