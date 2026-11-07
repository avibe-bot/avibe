# CLI read-command startup and Session Run lookup

## Change contract

Performance only: preserve CLI arguments, output, admission/ACL decisions,
Session ownership, ordering, pagination, and every persisted row. Keep the lazy
CLI dispatcher introduced in #3309. No native client, IPC change, Vault policy
change, runtime cache, or new background mechanism.

- `core.handlers` and `core.handlers.model_hub` expose their existing public
  names lazily. Importing leaf identifiers no longer initializes handlers,
  Agent SDKs, or the Model Hub service.
- Move the existing request ContextVar and presence check into
  `vibe.ui_request_context`. The Web adapter still sets/resets the same context;
  non-HTTP authorization checks no longer import FastAPI to discover that no
  HTTP request exists. HTTP identity/role resolution is unchanged.
- Migration `20261107_0075` adds indexes on `agent_runs.callback_session_id` and
  `(source_kind, source_actor)`. Together with the existing execution-Session
  index, all three arms of the existing Session-ownership OR predicate become
  index searches. The query and enrichment code are unchanged. Downgrade removes
  only these indexes; metadata-built databases and repair replays are supported.

## Evidence (2026-11-07)

Mac arm64, CPython 3.15.1, identical dependencies, fresh interpreter per command.
Baseline: `4542c7684`; candidate: this change. Both use separate, test-owned
initialized homes with identical synthetic data: 60,000 Runs, one Session and
Show Page, 36 small Skills, and current readiness receipts. No live service is
started. Timings are not CI budgets. Wall time is the median of three runs;
import time is the sum of `-X importtime` self-times from a separate run, not
cumulative times (which would double-count nested imports).

| Command | Wall before (s) | Wall after (s) | Imports before (s) | Imports after (s) |
| --- | ---: | ---: | ---: | ---: |
| `version` | 0.060 | 0.065 | 0.037 | 0.039 |
| `vault fetch` (service unavailable) | 0.077 | 0.083 | 0.055 | 0.053 |
| `vault list` | 0.339 | 0.252 | 0.250 | 0.184 |
| `watch list` | 0.303 | 0.215 | 0.216 | 0.150 |
| `task list` | 0.320 | 0.245 | 0.222 | 0.170 |
| `harness status` | 0.343 | 0.292 | 0.263 | 0.207 |
| `show status` | 0.701 | 0.247 | 0.552 | 0.177 |
| `agent list` | 0.283 | 0.239 | 0.215 | 0.163 |
| `session queue list` | 0.331 | 0.269 | 0.254 | 0.192 |
| `data query --sql 'select 1'` | 0.277 | 0.210 | 0.212 | 0.156 |
| `skill list` | 0.273 | 0.175 | 0.188 | 0.120 |
| `runs list` | 0.364 | 0.254 | 0.260 | 0.202 |

The config/repair path also loads model identifiers and previously pulled in
both eager package initializers. A fully ready database can bypass that path;
its ordinary store reads should not be credited with the larger config-path
saving. `show status` still exercises config loading in the table above.
Version and thin Vault fetch are intentionally unchanged. The latter measures
an unavailable test service, not authenticated fetch latency.

The warmed synthetic Run page (median of seven store calls, imports excluded)
falls from 12.9 ms to 2.4 ms with identical returned IDs. Its plan changes from
`SCAN agent_runs` to three `SEARCH agent_runs` arms under `MULTI-INDEX OR`.
Sorting still applies to the matching Session rows; this change does not add
an ordering mechanism. The synthetic fixture is not a measurement of a live
installation's much larger payload I/O or its cold disk cache.

## Regression coverage

- Fresh-process CLI tests execute all ten read commands against real isolated
  state, including Runs, Tasks, Watches, a Session, Show Page, and a Skill. They
  require successful output and an empty intersection with the forbidden
  backend/SDK/server module set. The pre-fix Vault-list case fails on imported
  Claude/MCP modules, not a timing threshold or an early command error.
- The real paginated Run-store query must search each ownership arm and never
  scan `agent_runs`; overlap is returned once, callback actors are not mistaken
  for dispatching Agents, and page boundaries remain unchanged. This test fails
  on the baseline's full scan.
- Migration round-trip preserves rows and restores the prior index schema.
  Existing migration replay/schema-stamping tests cover idempotent upgrade.
- Existing HTTP/CLI invocation-authority scenarios, resource ACL, Model Hub,
  Show, pagination, and read-projection tests cover the unchanged consumers.

Production upgrade/restart and post-upgrade live timings remain owner-operated
verification. This work does not change the running service or migrate its data.
