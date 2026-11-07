# Live Task/Watch definition caches

## Contract

The SQLite-backed scheduler and Watch supervisor retain only nonremoved,
nonretired definitions. Paused definitions remain resident. Retirement is not
removal: by-id reads, public list/count filters, Run history, and the existing
`finished` versus `removed` presentation do not change. Result/notice writers
and explicit mutations can still read a retired definition by primary key,
without inserting it into the cache. A successful retirement evicts immediately;
resuming a Watch clears its retirement marker and republishes it normally.

Watch startup confirmation reads the named definition and its keyed runtime
projection, off the event loop. It never refreshes the shared definition mirror
or loads every runtime entry. The timeout, stable-running window, quick quiet
completion, and failure handling retain their existing contracts.

## Implementation and caller audit

- `ScheduledTaskStore.load` and `ManagedWatchStore.load` request
  `include_retired=False`. The filter runs in SQLite, before lifecycle projection
  or enrichment. All their `maybe_reload`/`data_version` callers, controller
  runtime-work reconciliation, result-write refreshes, and refused/failed-write
  recovery therefore share the smaller reload.
- Index-only migration `20261107_0074` adds
  `(definition_type, created_at, id) WHERE deleted_at IS NULL AND retired_at IS NULL`.
  The query-plan test requires an index `SEARCH` and no temporary ordering tree.
  Upgrade adds no facts or backfill; downgrade drops only the index. Models and
  head-schema index repair declare the same index.
- Core by-id misses read retired rows without caching them. An incomplete mirror
  after a failed reload retains its existing fail-closed behavior until reload
  succeeds; it does not issue another read from `get_*` during that outage.
- Public SQLite list/get methods retain their defaults. CLI `show`, paginated
  lists, UI lists, counts, and explicit mutation validation still see history.
- `list_watches_for_recovery` deliberately keeps its strict full historical
  snapshot. It is used only while reconciling persisted stale worker identities,
  not for ordinary definition reloads or startup polling. Reducing that snapshot
  would change the process-recovery contract; it is not part of this change.
- Legacy JSON file stores keep their full-file read/write contract. Filtering
  those mirrors would make an ordinary save delete historical definitions.
- No retention/deletion policy, table-specific invalidation mechanism, service
  restart/deployment, or Session/Model Hub cache change is included.

## Reproduction

Run `python scripts/bench_definition_caches.py --samples 5` from an installed
source-development environment. The script creates a temporary home and SQLite
DB, clears inherited Avibe caller/credential settings, and starts a private
owner-only control socket. It runs the real runtime-work supervisor and Watch
service, then measures fresh CLI processes through the real control endpoint.
The waiter sleeps one second and exits with the quiet no-event contract; no
Agent backend or external account is called. A separate SQLite connection
commits every 20 ms throughout the end-to-end samples.

Fixture: 3,800 Watches (3,500 retired, 300 paused/live), 4,000 Tasks (3,500
retired, 500 paused/live), 1 KiB synthetic metadata per definition. Paused live
rows exercise the residency predicate without launching hundreds of processes.
The benchmark does not claim to reproduce the reporter's saturated 6 GB DB or
50–110 concurrent Agent Runs.

Measured on macOS arm64, Python 3.13.11, five sequential samples per revision;
base `952859064` already includes the definition/status Run index from #3339:

| Measurement | Before | After |
| --- | ---: | ---: |
| Watch reload, median | 64.79 ms | 5.44 ms |
| Task reload, median | 57.22 ms | 7.15 ms |
| Resident Watches | 3,800 | 300 |
| Resident Tasks | 4,000 | 500 |
| `watch add` CLI-to-confirmation, median | 2.070 s | 1.294 s |
| Service process CPU during add, median | 1.725 s | 0.449 s |
| All Watch mirror reloads during add, median | 19 | 9 |

The remaining reloads are the supervisor/mutation paths, not startup polling;
each now reads the 300 live definitions rather than 3,800 total definitions.
The startup regression test independently prohibits both a definition reload
and a full runtime load while another connection retires the watched row.

## Validation

- Four new residency/startup regression cases fail on the base for their intended
  reasons (historical rows resident; retirement not evicted; startup reload).
- 952 tests pass across residency, Watch CLI/control IPC, scheduler, supervisor,
  lifecycle, and job-Watch suites.
- A further 305-test focused run covers the new cases, outage recovery,
  migration/index repair, Task CLI, and public payload enrichment (some overlap).
- Explicit migration upgrade/downgrade test preserves the retirement row, and
  the existing head-schema repair test verifies the new index is restored.
- Changed Python files pass Ruff. Tests use hermetic state; no live runtime or
  regression deployment was changed.
