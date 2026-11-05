# CLI runtime path

Status: **draft investigation; paused before measurement** (2026-11-06).
This is a plan, not authorization to implement or deploy. The owner decides.

## Decisions requested

Pending the command inventory and hermetic baseline. The leading direction is:
remove redundant startup work first, then move high-frequency mutations behind
existing private control IPC without weakening caller authority. Keep ordinary
reads genuinely read-only. Do not promise that SQLite's single writer can avoid
all serialization.

## Change contract

- Docs only: no runtime, schema, migration, authority, Vault or deployment changes.
- Ordinary CLI overhead must not hold the controller loop or repeatedly acquire
  migration/writer locks when state is already current.
- Commands never operate on unsupported/old schema; rejected remote callers never
  become the controller's local Owner.
- Live installation remains untouched by benchmark writes; use synthetic
  HOME/AVIBE_HOME/XDG, credentials and loopback-only fixtures.

## Verified starting points

Source baseline: `7a21c97df` (default branch fetched 2026-11-06).

| Finding | Code / related work | Consequence |
| --- | --- | --- |
| `ensure_sqlite_state` caches only in-process, then takes `MigrationFileLock`, runs Alembic, validates backup lifecycle, runs data migrations and counts | `storage/importer.py::ensure_sqlite_state` | A new CLI process repeats bootstrap; calling it a cheap precondition does not make it cheap across processes. |
| SQL query execution is guarded/read-only, but its default entry bootstraps first | `storage/read_only_query.py::run_read_only_query` | A read-only SQL statement is not yet a read-only command. An explicit `db_path` skips that bootstrap in diagnostic extraction. |
| `main` resolves invocation authority before argument parsing; remote provenance reads configured pairing and durable binding | `vibe/cli.py::_cli_invocation_authority`, `storage/resource_access_service.py::resource_user_context_from_metadata` | Profile local and remote callers separately; do not remove authorization to make dispatch faster. |
| `--version` is not a declared parser option; the supported command is `version` | `vibe/cli.py::build_parser` | Record exit status, and profile both the requested `--version` error path and successful `version`; do not report the former as a successful operation. |
| Engines set a 5-second busy timeout; non-read-only connections set WAL mode | `storage/db.py::create_sqlite_engine` | A read through a writable engine is not equivalent to a read-only connection. |
| #3280 removes completed-v3 migration writer acquisition and moves diagnostic counts out of that transaction | PR #3280, open; reviewed head `8110e7c7e0a6ea17239a500b3d39d93d5618233e` | Useful independent first fix; it does not remove the file lock, Alembic or backup-validation work. |
| #2725 and #2706 are issues, not PRs; #2726 is a closed docs-only design PR | `docs/plans/controller-loop-sqlite-writers.md` | A warm service alone does not solve loop-side blocking. Preserve ordered persistence and cancellation/activation semantics when moving writes off-loop. |

## Work remaining before decision

1. Complete all parser leaves and dynamic MCP inventory: state transport,
   direct reads/writes, ensure calls and concrete mutations.
2. Finish frequency extraction for the fixed window
   **2026-11-04 15:56–2026-11-05 18:56 UTC** (27 hours). Parse actual tool-call
   arguments, exclude quoted examples, deduplicate mirrored calls and cross-check
   `messages`/`agent_runs`; publish aggregates only.
3. Profile `--version`, supported `version`, and top ten actual command paths in
   fresh processes using synthetic state. Separate import, authority, parser,
   bootstrap, business logic and IPC/network waiting. Record successful versus
   deliberately isolated/refused paths honestly.
4. Compare current-schema fast path, lazy imports/bootstrap, warm mutation
   services, truly read-only diagnostics and remaining-writer retries. State
   costs, risk, expected/measured benefit and service-down behavior.
5. Incorporate the Vault design: private IPC; DB-free `fetch`; server-only Show
   adapter; preserved remote/page identity; durable pre-use intent; fail closed
   before handoff; no replay after possible external use; separate `run` process
   transport. An audit-operation migration and authority changes need explicit
   owner approval.
6. Define ordered, independently shippable/reversible PRs; do not duplicate
   #3280 or the separate canonical whole-transaction retry lane.
7. Specify the before/after acceptance benchmark: 200 mixed fresh CLI calls,
   20-way concurrency, synthetic transcript commits, zero surfaced lock errors,
   per-command p50/p95 overhead, turn commit p50/p95/max, writer occupancy,
   authority/upgrade/failure regressions and Vault no-replay accounting.

No performance results or Merge-ready claim are made by this draft.
