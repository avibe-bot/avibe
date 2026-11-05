# CLI runtime path and state architecture

**Status:** decision plan; docs-only lane, 2026-11-06. No runtime or schema implementation is included here. The owner decides which implementation PRs proceed.

## Decisions the owner must make

1. **Approve the shape:** a minimal/lazy CLI front end, a read-only current-state fast path, and warm-service delivery for hot mutations. The service is the normal path; it is not a reason to add a second proxy or to make the controller a workspace proxy.
2. **Approve the security boundaries:** explicit caller provenance on control-IPC requests; fail closed when the service is unavailable for service-owned mutations; Vault's durable pre-use intent and post-use uncertainty semantics; any additive audit-operation migration.
3. **Do not use the following as the solution:** increasing SQLite timeouts, a broad global writer, disabling Agent Relay refresh, or relying on #3280/#3281 as a throughput fix. #3280 and #3281 are valuable foundations, but the cold-start/bootstrap path remains.

**AI Relay automatic refresh stays part of normal behavior.** The temporary panel-refresh pause used during the incident is an operational mitigation only; it must not become the architecture or acceptance benchmark.

## Executive recommendation

Split the current `ensure_sqlite_state()` contract into two explicit operations:

- `probe_current_sqlite_state()`: one bounded read-only check of the exact Alembic revision, required data-migration completion markers, and a migration-ready stamp. It takes no migration file lock, opens no writable engine, does no backup bookkeeping, and does no report counts. An unknown, missing, malformed, old, or in-progress state falls back to the existing conservative repair path.
- `repair_sqlite_state()`: the existing lock/migration/import/backup path, used only when the probe says the state is not ready or when an explicit maintenance operation requests it.

Then:

- make `version`, help, parse errors, and static command admission lazy and database-free for local/public commands;
- make direct read commands use an explicit SQLite read-only connection and remove Agent-model prefill from read paths;
- move Agent enqueue, Session operations, Task/Watch mutations, and Vault preparation/delivery behind the existing private control IPC, where one warm service owns the engine and writer admission;
- leave arbitrary SQL diagnostics as guarded read-only SQLite, with no bootstrap write;
- retain bounded whole-transaction retries for residual direct writers and never retry an external effect.

This is the smallest complete architecture that addresses both observed costs: every fresh Python process and every fresh state bootstrap. A warm SQLAlchemy pool alone is not sufficient while each Agent still creates a new Python process and takes the global bootstrap path.

---

## 1. Evidence and measurements

### 1.1 Owner-host measurements already established

These measurements are from the owner's Mac under load and are useful as operational context, not as a hermetic benchmark:

| Invocation | Observed wall time |
| --- | ---: |
| `python -c pass` | 0.01 s |
| `python -c "import vibe.cli"` | 0.31 s |
| `vibe --version` | 0.9–4.6 s; this is currently an invalid CLI spelling, not a successful version command |
| `vibe show status` | 1.6 s |
| `vibe harness status` | 2.3 s |
| `vibe runs list` | 8.3 s |

The gap is therefore not explained by imports alone. `main()` authority/admission and command bootstrap dominate many real calls, especially under concurrent SQLite load.

### 1.2 Instrumented orchestration baseline

The strongest cold-start evidence is the archived benchmark at `archive/sqlite-vault-contention-2026-11-06/benchmark/REPORT-FINAL.md` and `PRIMARY-RESULTS.md`:

| Property | Baseline |
| --- | --- |
| Source | `base5b2ed0c7` / full `5b2ed0c7b33052c7b4f7ba6c749752abe9401557` |
| Workload | 50 concurrent fresh Python processes; 1 MiB synthetic DB; 20 Hz real transcript writes |
| Vault branch | Standard, no approval; fixed loopback custody HTTP stub; actual `cmd_vault_fetch` orchestration |
| Import-to-command p50 / p95 | **29.389 s / 48.001 s** |
| Whole-subprocess wall p50 | **29.712 s** |
| Import p50 | **6.770 s** |
| Bootstrap p50 | **22.465 s** |
| Coverage limits | Does not exercise full CLI `main()` dispatch, real avault/Keychain/cryptography, protected approval, or production network latency |

This is an **instrumented orchestration baseline**, not a claim that every deployed Vault request normally takes 29 seconds. It is one sample with instrumentation, host load, and serialized bootstrap contention. It does directly support removing cold process/bootstrap work from the hot path.

The same archived run measured the merged foundations:

- #3280 (`80c399b349...`, current RC12): in the fixed-rate synthetic workload, transcript overlap p95 improved from 922.320 ms to 4.001 ms on the small DB and from 880.006 ms to 3.687 ms on the history DB; bootstrap `BEGIN IMMEDIATE` fell from 50 to 0 and COUNT statements inside the writer transaction from 400/400 to 0/400.
- #3281 (`dea1fba56...`): the controlled benchmark saw no timeout before or after, so it does **not** measure retry recovery or prove a speedup. Its deterministic contention regressions prove the intended recovery behavior separately.
- Across the primary conditions: 400 Vault fetches/audits, 6,075 transcript writes, and zero observed SQL errors in the synthetic databases. Protected/cryptographic paths were not covered.

### 1.3 Read-only frequency reconstruction

Window: **2026-11-04 15:56:00 UTC through 2026-11-05 18:56:00 UTC** (27 hours). The state database was opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; no `vibe` command was used for extraction because the default `vibe data query` path itself currently calls `ensure_sqlite_state()`.

`agent_events` is the execution source of truth for Agent Bash tool calls. `messages` and `agent_runs` are cross-checks, not additive counts:

- 9,099 `agent_events` with `event_type='tool_call'`; 1,515 actual `vibe` invocations were extracted from rendered Bash command JSON using a shell AST parser.
- 9,042 `messages` in the interval contained no separately persisted rendered tool-call rows; counting them again would double-count or invent calls.
- 1,228 `agent_runs`: 531 `agent_run`, 386 `watch`, 241 `watch_runtime`, 67 `scheduled`, and 3 `task_escalation`. These are durable activity populations, not a one-to-one CLI invocation count.

| Rank | Command leaf | Count |
| ---: | --- | ---: |
| 1 | `vibe agent run` | 363 |
| 2 | `vibe data query` | 184 |
| 3 | `vibe harness status` | 123 |
| 4 | `vibe watch add` | 100 |
| 5 | `vibe watch list` | 92 |
| 6 | `vibe skill load` | 52 |
| 7 | `vibe runs list` | 41 |
| 8 | `vibe session update` | 35 |
| 9 | `vibe session queue list` | 25 |
| 10 | `vibe vault run` | 23 |
| 11 | `vibe task add` | 17 |
| 12 | `vibe show status` | 13 |

The count is noisy by design: it counts Agent-issued CLI calls across Sessions, Watches, and delegated work, not human intent. It is still enough to prioritize the hot paths. In particular, `agent run`, `watch add`, `session update`, and `vault run` are not rare administrative commands.

### 1.4 Hermetic cold-start profile

Source: RC12 worktree after #3280/#3281, CPython 3.12.12, a fresh synthetic `AVIBE_HOME`, no inherited `AVIBE_*` caller provenance, no service, no credentials, and a current-schema copied test DB. Each case was a fresh Python process under `cProfile`; wall time includes the profiling wrapper and is therefore instrumentation-inflated. The profile was run against scratch state only.

| Command | Wall s | cProfile: `vibe.cli` import | Authority/admission | `ensure_sqlite_state` | Handler | Scratch result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `version` | 0.499 | 0.372 | ~0.001 | — | ~0.000 | success |
| `--version` | 0.473 | 0.358 | ~0.001 | — | — | `invalid_arguments`; unsupported spelling |
| `agent run` | 0.717 | 0.363 | 0.169 | 0.050 | 0.058 | refused: Agent absent in scratch catalog |
| `data query` | 0.700 | 0.357 | 0.164 | 0.050 | 0.053 | guarded read succeeded |
| `harness status` | 0.757 | 0.362 | 0.169 | 0.050 | 0.081 | read succeeded; controller unavailable |
| `watch add` | 0.677 | 0.362 | 0.174 | — | 0.002 | refused before store: no Session policy in standalone scratch |
| `watch list` | 0.735 | 0.378 | 0.167 | 0.050 | 0.062 | read succeeded |
| `skill load` | 0.779 | 0.358 | 0.180 | — | 0.061 | filesystem load succeeded; observation IPC unavailable |
| `runs list` | 0.843 | 0.434 | 0.187 | 0.055 | 0.065 | read succeeded |
| `session update` | 0.785 | 0.384 | 0.185 | 0.056 | 0.061 | refused: synthetic Session absent |
| `session queue list` | 0.718 | 0.369 | 0.167 | 0.050 | 0.055 | refused: synthetic Session absent |
| `vault run` | 0.769 | 0.359 | 0.167 | 0.048 | 0.090 | refused: synthetic secret absent |

The exact scratch downstream arguments are deliberately limited: no live Session identity, Agent catalog, secret, or service was copied into the test home. The important common result is that `vibe.cli` import alone consumes about 0.36–0.43 s of cProfile cumulative time, authority/admission another ~0.16–0.19 s on stateful commands, and current-state bootstrap about 0.05 s with no contention. Under 50-way cold contention, the same bootstrap stage becomes the 22.465 s p50 measured above.

The profile artifacts used to derive this table are in `_tmp/cli-runtime-path/profile-results/`; they are scratch evidence, not a repository dependency. The full inventory is below and was assembled from the parser/dispatch source; branch-specific uncertainty is called out rather than hidden behind a family-level label.

---

## 2. Current command-to-state inventory

### Legend

- **E**: `ensure_sqlite_state()`; in a fresh process this currently includes the migration file lock, Alembic check/upgrade, backup lifecycle tracking, JSON/data-import checks, and report work. #3280 removes one completed-v3 no-op writer but not the whole path.
- **A**: `VibeAgentStore`; E plus model catalog synchronization/prefill that can UPDATE missing Agent rows.
- **B**: `SQLiteBackgroundTaskStore`; E plus background definition/index readiness.
- **S/V/P/SE**: Session, Vault, Show Page, or Show Session Event engine helpers; each reaches E and then a normal writable engine.
- **RO**: explicit read-only SQLite connection (`mode=ro`, `query_only`); this is different from a normal SQLAlchemy engine that happens to execute SELECTs.
- **IPC**: existing private controller control channel, not the browser UI port.
- **UI HTTP**: loopback Show/UI HTTP notification or event route.
- **Shared source refs:** `vibe/cli.py:19718-19896`, `storage/importer.py:93-219`, `storage/db.py:33-73`, `core/vibe_agents.py:740-777`, `storage/background.py:3399`, `vibe/internal_client.py:53-137`, and `storage/read_only_query.py:182-269`.

### Entry and service lifecycle

**Primary sources:** `vibe/cli.py:14376-17260`, `vibe/cli.py:17374-19896`, `vibe/runtime.py`, `vibe/remote_access.py`, `vibe/upgrade.py`.

| Leaf | State path | IPC/files/network | Writes or contention |
| --- | --- | --- | --- |
| bare `vibe`; `start` | Default-state guard; service/UI initialize their own state | Service/UI startup, PID/status/runtime files, optional browser; may start integrations | Creates directories/config/status and starts processes |
| `stop` | No ordinary command-body E identified | Service/UI/backend stop and cleanup; runtime files | Stops processes and writes status/cleanup |
| `restart` | Supervisor/startup path | Immediate or delayed restart supervisor; runtime files | Schedules or performs restart; not a read |
| `status` | No command-body E identified | Reads status/restart/internal-server files and process/runtime state | Runtime helper may clean stale lifecycle artifacts; not a pure DB read contract |
| `desktop endpoint` | Default-state/config guard | Reads/writes desktop endpoint/config artifacts | May create missing config |
| `desktop remove-backends` | Follow-up lifecycle path | Managed backend process/install removal | Removes managed backend resources |
| `doctor` / `doctor --fast` / `doctor --deep` | A and B paths can reach E; direct revision checks also occur | Config/log/runtime/process/install probes; deep mode can reach network | Writes doctor report; stale runtime cleanup and probes may mutate files |
| `doctor repair home-migration`, `stale-install-runtime`, `stable-launchers`, `duplicate-service-processes`, `stale-restart-state`, `askill`, `avault`, `model-hub-engine`, `git-runtime`, `show-runtime`, `tmux` | Depends on target; repair may initialize state | Runtime, launcher, managed dependency and network operations | Explicit repairs, installs, deletes, process starts/stops; not a normal hot path |
| `version` | None | Version string only | No state write |
| `--version` | Invalid parser option | Error after current global import/authority entry | No state write; do not treat it as `version` |
| `check-update` | No ordinary SQLite path identified | Release/version network lookup; desktop-managed variant | Potential cache/installation effects are downstream-specific |
| `upgrade` | Activation/startup path | Downloads, verifies and atomically activates packages/generations; schedules restart | Installation, launcher, generation, runtime mutations |
| `runtime status` | No direct E identified | Managed runtime/Git/runtime files and probes | Downstream status helpers need to remain side-effect-free |
| `runtime prepare` | Dependency-specific | Downloads/prepares Show runtime, askill, tmux, Git, ripgrep, cloudflared, avault, model hub | Managed binary/cache/config writes |
| `runtime clean` | No direct E identified | Managed runtime cleanup | Deletes unused generations/caches under keep rules |
| `remote` setup; `remote pair` | Pairing service can reach E; owner check before pairing | Pairing network, identity/config/binding state | Durable pairing/authorization/config writes |
| `remote status` | No direct E identified | Config/PID/connector state; may probe local status | May unlink stale connector PID/state files |
| `remote start` / `remote stop` | Service/runtime path | cloudflared lifecycle, tunnel network, runtime files | Starts/stops connector and writes lifecycle markers |
| internal `__restart-supervisor`, `__activate-upgrade`, `__activate-install` | Owner-only internal paths | Supervisor/activation/install files and subprocesses | Lifecycle/installation mutations; never remote-agent alternatives |

### Agents, Bots, and model selection

**Primary sources:** `vibe/cli.py:5398-7141`, `core/vibe_agents.py`, `core/bots.py`, `core/scheduled_tasks.py`.

| Leaf | State path | IPC/files/network | Writes or contention |
| --- | --- | --- | --- |
| `agent list`, `agent show`, `agent models <name>` | A: E plus model-prefill UPDATE and reads | Agent/resource ACL reads; backend catalog may read config/network | Apparently read-only Agent discovery can write missing model rows |
| `agent models --backend vibey` | Backend/config read; no Agent-store constructor in this branch | Model Hub config/menu | No identified state write |
| `agent models --backend claude/codex/opencode` | Backend-specific | Model snapshot/config/server lookup; refresh behavior differs | May use cache/network; not uniformly file-only |
| `agent create`, `agent update`, `agent enable`, `agent disable`, `agent remove`, `agent import` | A; Agent/policy writes | Prompt files and global Agent files; catalog/config reads | Creates, updates, enables, archives/rewrites references, or imports Agent definitions |
| `agent default` | A | Builtin synchronization if needed | Writes `state_meta` default and may create builtin Agent |
| `agent run` new Session | A + B; Session/scope/ACL reads and durable run/session/message enqueue | No live dispatch IPC in the current handler; execution later belongs to service | High-frequency SQLite enqueue; failed reservation rolls back |
| `agent run --session-id` | A/B plus Session reads; queue/steer durable intent | Service later consumes stored delivery | Queue/steer write |
| `agent run --fork-self` / `--fork-session` | A/B plus source Session reservation/copy | Native context and working-directory/file semantics | Creates/forks durable Session and Run state |
| `agent run --bot` | Bot service + A/B | Bot home/Session and permanent routing | Owner Bot checks; directory/Session/run writes |
| `agent run --sync` | Same enqueue plus polling | Polls durable Run state | Does not create a separate live execution path |
| `bot list`, `bot show` | A; main-Bot ensure can create/repair Agent and Session | Bot directory, optional Git/event publication | A read can create Bot home/Session |
| `bot create`, `bot set`, `bot update`, `bot remove` | A + Bot service | Bot directory, metadata and event publication | Bot designation/metadata/Session lifecycle writes; removal is not assumed to delete history |

### Runs and Harness

**Primary sources:** `vibe/cli.py:3971-5337`, `vibe/cli.py:7269-7580`, `storage/background.py`, `core/scheduled_tasks.py`, `core/watches.py`.

| Leaf | State path | IPC/files/network | Writes or contention |
| --- | --- | --- | --- |
| `runs list` | B E; Run ledger read | Direct SQLite handler | Read command still performs bootstrap work |
| `runs show` | B E; Run read | Best-effort controller `turn_state` IPC for live enrichment | No intended business write |
| `runs cancel` | B E; Run cancellation write | Live Run cancellation IPC when applicable | Durable cancellation/result metadata write; must not replay work |
| `harness status` | B E; active Runs/definitions read | Controller `list_running_agents` IPC merged with persisted state | Current handler still bootstraps before a mostly read operation |
| `task list`, `task show` | B E; definition/history read | Direct SQLite | No intended definition write beyond bootstrap/index readiness |
| `task add` pure command (`--shell`, `on_failure=none`) | B E; definition write | Validates cwd/command/schedule; does not execute command | Definition insert |
| `task add` message/escalation; `task update` Session-bearing | A/B plus Session placement; definition write | Message file/stdin, Session reservation/callback metadata | Definition and possibly Session reservation writes |
| `task update` pure command | B E; definition write | Schedule/command/cwd/timeout fields | Definition update |
| `task pause`, `task resume` | B E; enabled/projection write | Controller observes durable change | Definition/runtime projection mutation |
| `task run` | B E; enqueue Run | Service later executes | Durable execution enqueue |
| `task remove` | B E; removal/retirement write | Store settles linked state | Definition removal/retirement |
| `watch list`, `watch show` | B E; definition/history read | Direct SQLite | Frequent reads pay bootstrap |
| `watch add` | A/B + optional Session reservation; definition write | Stores waiter, timing, retry and follow-up; controller later runs waiter | Definition and possibly Session reservation writes |
| `watch update` | B and possibly A/Session reservation; definition write | Concurrency/job-watch restrictions | Definition/reservation mutations |
| `watch pause`, `watch resume` | B E; enabled/runtime projection write | Controller reconciliation changes worker | Definition/runtime writes |
| `watch remove` | B E; removal/retirement write | Store settles linked state | Definition removal/retirement |

### Sessions and data

**Primary sources:** `vibe/cli.py:7593-8351`, `storage/read_only_query.py`, `core/services/sessions.py`, `storage/agent_transcript.py`.

| Leaf | State path | IPC/files/network | Writes or contention |
| --- | --- | --- | --- |
| `session list`, `session get` | S E; Session/project/resource reads | Shared Session service in-process; no controller IPC | Read path still opens writable engine after E |
| `session update` | S E; Session writer | Post-commit UI HTTP activity notification | Title/visibility/scope placement write plus bootstrap contention |
| `session send-now` | S E; access read before IPC | Controller `send_now` promotes exact FIFO head | No new work, but CLI still bootstraps first |
| `session queue list` | S E; queued delivery read | Web/Workbench target ACL | Read path still bootstraps |
| `session queue remove` | S E; queue/run retirement write | Post-commit UI HTTP notification | Retires queued message and linked Run |
| `data query` inline/file/stdin | E-transitive, then guarded RO connection | Temporary views, authorizer, step limit; optional file reads | Preceding E is write-capable today; SQL body itself is RO |
| `data cache` | Explicit `db_path` skips E; guarded RO query | Reads cache/price files | No identified refresh/write in handler |
| `data skill-usage` | Direct E; statistics read | Config/skill statistics | `--clear --yes` deletes/clears statistics |
| `data retention` plan | Direct E; retention read | Config/policy and trace inspection | No intended write in plan branch |
| `data retention --run/--compact` | Direct E; retention writer | Lease/progress/trace deletion/checkpoint paths | Deletes history/compacts under safety conditions; never a hot Agent command |
| `model-requests list`, `show` | No command-body SQLite path identified | Reads private artifact metadata/files | Read-only file path |
| `model-requests export` | No command-body SQLite path identified | Copies request artifacts to explicit destination | File write |

### Show Pages

**Primary sources:** `vibe/cli.py:15350-16290`, `core/show_pages.py`, `core/show_session_events.py`, `core/show_git.py`.

| Leaf | State path | IPC/UI/files/network | Writes or contention |
| --- | --- | --- | --- |
| `show list` | P E; page/access read | Page payload/config/public URL files | Read path still bootstraps |
| `show path` | P E; `store.ensure` can create row | Creates workspace directory; may UI-HTTP prewarm | Page/directory/runtime side effects |
| `show status --brief` / full | P E; page read | Full adds filesystem/Git checkpoint status | No intended page write, but E remains |
| `show update` (rotate/share-id/visibility) | P E; page write | UI prewarm unless offline | Share/access/visibility writes and runtime notification |
| `show mark`, `show reply` | P/SE E; page/event writes | Body file/stdin; UI event route or local fallback | Assistant mark/reply event writes |
| `show marks` | SE E; mark projection read | Direct store | Read path still bootstraps |
| `show unmark` | SE E; mark read then resolution event write | UI/event path | Event write when a mark matches |
| `show event` | P/SE E; page/event/dispatch reservation | JSON/file input; UI HTTP; can enqueue Agent work | Event/dispatch writes; timeout must not blind-retry |
| `show annotate` | P/SE E; annotation/event processing | UI HTTP/screenshot/materialization | Event and possibly artifact writes |
| Show event-token helper | No SQLite requirement itself | `_load_or_create_show_event_secret()` can create a secret file; UI PID/prewarm checks | Secret-file write independent of DB |

### Vault

**Primary sources:** `vibe/cli.py:8747-11477`, `storage/vault_service.py`, `vibe/api.py`, `vibe/internal_client.py`.

All normal Vault metadata branches currently use V E. Key custody and cryptography remain in avault; the Python/CLI process must not receive key material.

| Leaf | Current state path | Current other path | Writes/effect |
| --- | --- | --- | --- |
| `vault list`, `find`, `tags` | V E; metadata reads | No plaintext delivery | Read path still bootstraps |
| `vault edit` | V E; metadata read/write | Releases affected Agent scopes; controller notification | Metadata/policy write |
| `vault remove` | V E; creates delete request | Approval/request notification | Request write, not unconditional deletion |
| `vault access` | V E; access-request write | Controller notification | Approval request |
| `vault request` | V E; provision request/poll | Optional spec file/stdin; notification/callback | Provision/request state writes |
| `vault run` | V E; grant selection/reservation/consumption/use audit | avault delivery and protected child process; arbitrary child filesystem/network effects | Secret delivery, audit/counter writes; no external replay on DB ambiguity |
| `vault fetch` | V E; grant/proxy-use audit | avault-authenticated HTTP; body/stdin and output file | Network egress plus audit/use-count writes |
| `vault sign` | V E or approval-request path | avault signing/approval context | Request/signing/audit state |
| `vault await` | V E; request/result polling | Approval/result continuation | Request/result state reads/writes as required |
| `vault inject` | V E; selector/grant/audit writes | avault writes selected output file | Secret delivery and audit |
| `vault export` | Compatibility/refusal surface; exact supported behavior must remain explicit | No plaintext shortcut | Must not reintroduce deprecated export semantics |
| `vault key authorize` | No normal CLI engine path | avault/native Keychain authorization | Native authorization effect |
| `vault key export` / `import` | V opened for audit after avault operation | Passphrase stdin; encrypted key document and atomic destination file | Key custody operation plus audit; owner-only boundary |

### Skills, Computer Use, debug, screenshot

**Primary sources:** `vibe/cli.py:705-1069`, `vibe/cli.py:16402`, `core/managed_skills.py`, `core/skill_observability.py`, `core/computer_cli.py`, `vibe/internal_client.py`.

| Leaf | State path | IPC/files/network | Writes or contention |
| --- | --- | --- | --- |
| `skill list`, `skill load` | No CLI E in handler | Skill catalog/files; best-effort controller observation IPC | `skill load` reads files and observation may write controller SQLite; do not disable observation as an optimization |
| `computer observe apps/windows/window` | No CLI E identified | Controller Computer Use IPC; optional screenshot file | Host observation/file output |
| `computer zoom` | No CLI E identified | Controller IPC; optional screenshot file | Host/image output |
| `computer activate/click/type/key/hotkey/scroll/drag/set-value/launch/force-quit` | No CLI E identified | Controller IPC | Host/UI mutations; exact driver effects are command-specific |
| `computer clipboard read/write` | No CLI E identified | Controller IPC | Host clipboard read/write |
| `computer run` | No CLI E identified | Script file and Computer Use bridge | Arbitrary host/UI effects |
| `computer js`, `computer js-reset` | No CLI E identified | Persistent Session JS kernel via controller IPC; optional code file | Kernel state/effects |
| `screenshot` | No CLI E identified | Host screenshot capture | Output-file write; no instance-role floor |
| `debug prompt export` | No direct store path identified | Prompt files and optional JSON context | JSON stdout |
| `debug request preview` | No CLI E; controller request-preview IPC | Private request artifacts | Diagnostic files; does not send model request |

### Raw MCP commands

**Primary sources:** `vibe/cli.py:19805-19896`, `vibe/mcp_cli.py`, `core/mcp_hub/runtime.py`, `core/mcp_hub/scope.py`.

`mcp`, `mcp-add`, and `mcp-discover` are dispatched before the ordinary parser. Their dynamic leaves must be inventoried by shape, not by a fixed tool-name list.

| Leaf/branch | State path | IPC/files/network | Writes/effect |
| --- | --- | --- | --- |
| `mcp --help` | No CLI E | Local usage only | None |
| `mcp --list`; `mcp <server>`/server `--help`; `mcp <server> <tool> --help` | No direct CLI store | Controller list/detail IPC, publication/schema files | Reads controller publication; service required |
| `mcp <server> <tool>` | No direct CLI store | Detail then call IPC; controller owns MCP stdio/HTTP session | External tool effects depend on tool |
| `mcp <server> --approve/configure/refresh/remove` | No CLI E in client | Controller approval/configuration/refresh/removal | Project/global MCP definition, connection/publication/cache writes |
| `mcp <server> --login/--login-complete/--logout` | CLI may read V2 config | Controller OAuth/network/token state | Auth state/token writes/removal |
| `mcp-add` / `mcp-add --json-stdin` | Local scope mapping reads; no identified E | Controller adds global/project definition and refreshes | `.mcp.json`, managed definition, connection/publication writes |
| `mcp-discover` | No CLI E | Controller discovery, native config reads, handshakes | Publication/connection registration |
| `mcp-add --help`, `mcp-discover --help` | No command body | Usage before owner admission, after global raw entry | None |

**Inventory conclusion:** the current CLI is mixed-mode. It is not a thin service client. It combines process-wide imports and authority, writable bootstrap, Agent/background/Session/Vault/Show SQLite, private controller IPC, UI HTTP, local files, network calls, and child processes. The same top-level command name can have materially different state paths by branch.

---

## 3. Target architecture

### 3.1 Invariants

1. **No old-schema operation:** a command either proves the exact schema/data readiness contract or completes the conservative repair path before using state. Unknown/corrupt/missing markers fail closed; they never count as current.
2. **No routine writer from a read:** `version`, help, `data query`, `harness status`, list/show reads, and Agent discovery do not take a migration writer or repair model rows merely to answer a read.
3. **No controller-loop blocking:** SQLite transactions, migration locks, subprocess waits, and file/network waits stay off the controller loop. Preserve #2725/#2706 ordering and activation-boundary rules when moving a write.
4. **Authority is explicit:** a private socket authenticates the local installation, not arbitrary caller claims. Every service request carries validated provenance; Session ID, PID, or `source=cli` is never authorization.
5. **No hidden fallback:** service-owned mutations return a precise not-executed `service_unavailable`/`service_busy` result when the service is down. They do not silently take the old direct path or start a service, because that would reintroduce contention and split authority semantics.
6. **No external replay:** SQLite retry can repeat a whole idempotent database transaction before an external effect. After Vault/HTTP/child-process handoff may have occurred, an uncertain audit completion is marked unknown/degraded and is reconciled by operation ID; the external effect is never automatically replayed.
7. **One source of truth:** the controller/service owns live scheduling, Session steering, Vault preparation and runtime projections. CLI output is a client view, not a second scheduler or workspace proxy.

### 3.2 Option evaluation

| Option | Cost | Risk | Benefit | Migration path |
| --- | --- | --- | --- | --- |
| **A. Version/readiness stamp fast path** | Medium: exact marker contract, race tests, split importer API | Incomplete marker set or migration TOCTOU could authorize code against an unusable state | Removes file lock, migration pipeline and bootstrap writer from every current-state process; directly attacks the 22.465 s p50 bootstrap stage | Use existing `alembic_version` + required data markers first. Add a new `state_meta`/sidecar stamp only in a separately reviewed migration if the existing markers cannot express readiness |
| **B. Lazy imports and bootstrap** | Medium/high refactor of the monolithic `vibe.cli` module and parser dispatch | Import cycles, changed help/errors, authority regression | cProfile shows ~0.36–0.43 s in `vibe.cli` import before a handler; makes version/help and static errors close to interpreter cost | Add a small raw entry/parser and lazy-load one command module at a time; retain parser-backed leaf/admission tests |
| **C. Warm control-IPC clients** | High: request contracts, server handlers, identity, backpressure, service-down UX | Authority confusion, stale ACL, IPC overload, accidental controller proxy | Removes per-process DB/bootstrap and gives one warm engine/writer admission for the highest-frequency writes; Vault fetch can avoid 50 cold processes | Start with Agent enqueue + Session send/update/queue + Task/Watch mutations. Keep compatibility adapters and an explicit diagnostic/offline mode only if separately approved |
| **D. Read-only direct paths** | Low/medium: RO engines and no-prefill stores | Stale data or bypassed ACL if a read handler is not narrowed correctly | No writer, no WAL mode mutation, no bootstrap write; preserves useful standalone diagnostics | Introduce `ReadOnlyState`/`read_only=True` constructors; `data query` and list/show handlers use `mode=ro`; pin query plans for growing tables |
| **E. Remaining whole-transaction retries** | Low/medium; classify every residual writer | Retrying a non-idempotent or post-effect operation can duplicate work | Suppresses transient lock failures while preserving correctness; #3281 now covers canonical transcript/activity/outcome paths | Reuse #3281 retry helper; apply only to short DB-only transactions, bounded attempts/backoff, and add contention tests per owner |
| **F. Broad global ordered writer** | High and cross-cutting | Queue starvation/latency and false belief that all cross-process writers are solved | Helps controller-loop ordering and fsync stalls | Keep #2726/#2725 ordered-writer design for controller-owned durable emits; do not adopt it as the first CLI fix or as a substitute for RO/service paths |

### 3.3 CLI entry and schema readiness

**Entry path:**

1. A tiny entry module handles raw `--help`, `version`, parse errors, internal activation admission, and command-name classification. It imports only stdlib/parser/version code.
2. Parse the command before resolving expensive authority. Public facts (`version`, help, parse errors) do not need a database lookup. A declared remote caller is still never treated as local Owner: commands with a role floor resolve and validate the caller before dispatch; unknown/internal commands fail closed.
3. Lazily import the selected command implementation. `mcp` loads only `mcp_cli`; Vault loads only Vault modules; Show/Computer/upgrade modules are not imported for version/help.
4. Resolve caller provenance once per stateful invocation and attach it to the request context. Keep the current fail-closed behavior for missing/malformed/stale remote snapshots and pairing mismatch.

**Readiness probe:**

- Open the existing database read-only (`mode=ro`; never set WAL mode on this path).
- Read the exact Alembic head and every required data-migration/import marker in one bounded statement or bounded fixed set. Treat missing table, missing marker, malformed JSON, old/new unexpected marker, or database-open failure as **not ready**.
- Read an atomic migration-ready stamp. The migration path clears/advances the stamp before any schema/data change and publishes ready only after the final commit and backup transition. Recheck the stamp inside the read transaction; if it changes, discard the connection and enter the conservative repair path.
- The normal command keeps that read snapshot for the operation or obtains a fresh one through the service. This prevents a command from using a partially upgraded database. Migration tests must cover current, old, new, missing, corrupt, replacement, held-writer, held-migration-lock and concurrent-upgrade cases.
- Do not run backup lifecycle validation, history COUNTs, JSON import, or data repair from the ready path. Those belong to migration/maintenance, not a per-command precondition.
- A current-state probe is not permission to operate on an old schema. The implementation must fail closed on any uncertainty; the probe is an optimization, not a new migration policy.

### 3.4 Read-only command layer

Introduce explicit read-only variants rather than relying on convention:

- `create_sqlite_engine(read_only=True)` / raw SQLite `mode=ro` for direct reads; no parent creation, WAL pragma, migration, index creation, or model prefill.
- `VibeAgentStore(read_only=True)` that never calls `prefill_missing_models()`. If the catalog is absent, return an explicit not-initialized result or ask the service; never repair it during `agent list/show/models`.
- `SQLiteBackgroundTaskStore(read_only=True)` for list/show/status reads; no index/table readiness repair in the read path.
- `data query` validates and authorizes SQL, then directly opens RO state. Its safe SQL authorizer remains; the change is to remove the preceding E write-capable bootstrap from the ordinary current-state path.
- `harness status` may continue to merge a bounded RO persisted snapshot with live controller IPC. If IPC is unavailable, report `controller_unavailable` without trying to repair SQLite.
- Preserve resource ACL filtering and instance-role floors. RO does not mean unauthenticated or unrestricted.

### 3.5 Warm service/control IPC layer

Extend the existing private control channel with versioned request/response contracts. The service owns one warm state engine/pool and a bounded writer queue; this is not a browser/cloud proxy for the workspace.

Each request carries:

- protocol version and request ID;
- operation and arguments, with no arbitrary SQL or plaintext Vault material;
- local/remote provenance from the invocation context, including Session/Run identity when applicable;
- the current resource/user context or a server-verifiable reference, never an untrusted role string;
- deadline/cancellation and an explicit idempotency/operation key where an external effect exists.

The server revalidates current Session membership, Project/instance ACL, role, purpose and grant scope. It may cache non-security metadata, but not revocation-sensitive authorization. IPC authentication proves the request came from this installation; it does not make arbitrary same-user JSON claims trusted.

| Command group | Target path | Service-down behavior |
| --- | --- | --- |
| `agent run` (new/session/fork/Bot enqueue) | Service reserves Session/Run and performs the short durable enqueue; service owns the warm engine and admission | Return not-executed; no direct SQLite fallback |
| `session update`, `session send-now`, queue remove, and eventually list/get | Service owns Session authority and steering; read-only list/get may initially use RO direct path if that reduces migration risk | Mutations fail closed; reads may use explicitly authorized RO fallback only if the contract says so |
| `task add/update/pause/resume/run/remove`, `watch add/update/pause/resume/remove` | Service owns definitions, reservations, runtime projections and scheduler coordination | Return not-executed; do not create a second scheduler in the CLI |
| `runs list/show/cancel`, `harness status` | Start with RO direct lists plus bounded live IPC; move to service read endpoints only if profiling shows it is worthwhile | Read status reports controller unavailable; cancel fails closed if live control is unavailable |
| `vault list/find/tags/edit/access/request/await` | Service owns metadata/policy/request transactions; avault remains the custody boundary | No direct fallback for protected/security-sensitive paths |
| `vault fetch` | DB-free thin client to Vault service; server-side adapter resolves policy/grant/audit and invokes avault | Precise not-executed error; no direct SQLite fallback, no automatic service start |
| `vault run` | Shared authorization/audit service, with a separate process/stdio transport preserving caller cwd, environment boundaries, cancellation, signals, exit code and backpressure | Not-executed before handoff; after handoff, return actual/unknown result without replay |
| `skill load/list` | Keep local skill-file reads; send observation through the service's bounded observation path if it is a write | Do not disable observation silently; report/defer telemetry according to its existing contract |
| `mcp` | Already a control-IPC client; only make import/argument bootstrap lazy | Existing service-unavailable behavior, no local hidden server |

### 3.6 Vault chapter: adopted from the hot-path design

The authoritative proposal is `_tmp/sqlite-bookkeeping/vault-hot-path-design.md`; it is folded here without weakening its security conclusions.

1. **Delivery:** one shared warm Vault service over the existing private local control IPC. `vibe vault fetch` becomes DB-free. Show Runtime uses a server-only adapter, not one Python subprocess per HTTP call. The service owns a warm engine and invokes avault; it never receives plaintext keys.
2. **Audit ordering:** validate caller, policy, selected fields, grant and host/method constraints; commit a value-free `prepared` credential-use intent before giving avault permission to act; hand off outside SQLite; complete the ledger by operation ID afterward.
3. **Failure semantics:** if the pre-handoff intent cannot commit, do not hand off. If the external effect may already have happened and completion fails, do not replay the request; return the actual response when known plus explicit audit-degraded/unknown state and reconcile later.
4. **One-shot grants:** reserve atomically with its audit intent, keep the grant unavailable after an uncertain handoff, and retry only idempotent ledger transitions. Do not retry egress.
5. **Identity:** a local socket authenticates the installation, not arbitrary caller claims. Agent calls bind to durable Run/Session authority; Show Runtime preserves the authenticated originating page/request identity or an explicitly owner-authorized background identity. No broad ambient Controller Owner token and no browser-accessible credential proxy.
6. **`run`:** do not accidentally run child commands under the controller's cwd/environment or return decrypted environment values to the CLI. Preserve process ownership, stdio, cancellation, signals and exit code in a separate reviewed transport.
7. **Queueing:** bounded, fair service admission may protect Vault's own writes, but SQLite still has one physical writer and unrelated direct writers still contend. Do not claim a pool removes serialization.
8. **Migration:** the durable operation ledger/uniqueness state is an additive schema/security migration if current tables cannot express it. It needs owner approval, upgrade/downgrade evidence, crash/uncertain-outcome tests and no history deletion.

### 3.7 Residual direct writers and controller loop

- Keep #3280 and #3281 as landed foundations; do not duplicate their changes.
- Use #3281's whole-transaction bounded retry shape for remaining short DB-only writers. Retry the complete transaction, not an individual statement, and classify exhaustion visibly.
- Never retry model calls, Vault egress, HTTP POSTs, child commands, or any operation that may already have affected the outside world.
- Move synchronous SQLite commits off the controller loop through the ordered writer/runtime-work path described by #2725/#2706, preserving per-emit message IDs, transcript order, runtime activation boundaries and cancellation points.
- Do not resurrect the parked broad global-writer design merely because it is conceptually simple. Measure after the CLI/service changes.

---

## 4. Phased delivery

Each phase is independently shippable and reversible. The current document is the decision gate, not an implementation PR.

| Phase | Deliverable | Boundary / approval | Rollback and validation |
| --- | --- | --- | --- |
| 0 | **Already landed:** #3280 no-op bootstrap-writer removal; #3281 whole-transaction retries | No new schema; #3281 changed writer correctness but not throughput | Revert independently if regression; retain their focused contention tests and archived benchmark |
| 1 | Split raw CLI entry/parser from the monolithic import; lazy-load command handlers and backend modules | Admission/authority regression surface; no credential boundary, but must preserve fail-closed floors | Revert to old dispatcher; parser leaf coverage, `version/help/invalid` profile, local/remote authority tests |
| 2 | Current-state readiness probe and conservative repair fallback | If existing markers suffice, no schema migration; if a new readiness stamp is needed, separate additive migration and owner approval | Feature flag/kill switch to always use repair path; current/old/missing/corrupt/concurrent migration tests |
| 3 | Explicit RO engines/stores; remove Agent prefill and background index repair from read commands; make `data query` no-bootstrap | Resource ACL and data-exposure boundary; no Vault | Revert individual read handlers to service/old path; RO authorizer, query-plan and writer-contention tests |
| 4 | Control-IPC contracts for `agent run`, Session mutations, Task/Watch mutations, and live Run cancellation | **Security/authority boundary; owner approval required.** Service-down behavior becomes externally visible | Keep direct path behind an explicit diagnostic-only switch during rollout; identity matrix, crash/cancel, ordering and 200-call benchmark |
| 5 | Vault metadata/preparation service and DB-free `vault fetch`; Show server adapter | **Vault/security boundary; owner approval required.** No plaintext/key custody change without avault review | Keep old fetch only as an owner-approved emergency mode, never automatic; protected/standard, pre-use failure, post-use uncertainty and no-replay tests |
| 6 | Separate Vault `run`/`inject`/`sign` process and custody transports; durable operation ledger if required | **Vault + possible schema migration; owner approval required** | Per-operation compatibility and explicit protocol version; crash/stdio/cancellation/audit migration tests |
| 7 | Remaining direct writer cleanup, controller-loop ordered persistence, fair service queues and benchmark hardening | May touch runtime activation/loop invariants; coordinate with #2725/#2706 owners | Roll back command family independently; no global writer until measured need is demonstrated |

Every implementation PR must name: command families changed, caller identity contract, service-down result, write transaction boundaries, migration/recovery behavior, and the exact benchmark slice it improves. None should silently change Relay refresh policy.

---

## 5. Acceptance benchmark

Run on a hermetic synthetic home with no owner DB, credentials, Keychain, service restart or external network except explicitly owned loopback stubs. Compare: current RC12, #3280/#3281 combined baseline, each phase, and the final warm-service path.

### Workload

- 200 mixed Agent-style CLI calls, 20-way concurrency, weighted by the reconstructed table: `agent run`, `data query`, `harness status`, `watch add/list`, `skill load`, `runs list`, `session update`, `session queue list`, `vault run/fetch`, plus a smaller set of list/show reads.
- A simulated turn continuously appends transcript/result/activity records at a fixed offered rate; preserve the real `SQLiteTranscriptStore` path and record every offered, attempted, committed, retried and skipped operation.
- Include service warm and service cold-start phases. Restore the normal AI Relay refresh behavior for the final workload; a temporarily paused panel is a separate mitigation sample, not the acceptance run.
- For Vault, use standard and protected fixtures separately. The protected fixture must exercise approval/grant state without exposing key material; HTTP/child-command stubs must count external handoffs and reject duplicate operation IDs.

### Required measurements and gates

| Dimension | Acceptance condition |
| --- | --- |
| Correctness | Zero surfaced SQLite lock/timeout errors in the mixed workload; zero lost/duplicate transcript rows; all retry exhaustion visible; no swallowed audit deficit |
| Latency | Report p50/p95/max for whole process, import, authority/admission, readiness/bootstrap, IPC, handler, SQLite wait/commit, and external egress separately. Do not subtract percentiles. |
| Turn safety | Transcript commit p50/p95/max and controller-loop stall time remain bounded; no synchronous migration/SQLite wait on the loop |
| Version safety | Current, old, new, missing, corrupt, replacement and concurrent migration states prove no command operates on an old/unknown schema |
| Service failure | Service-owned mutations return not-executed with no direct-SQLite fallback or automatic service start; RO commands follow their documented standalone behavior |
| Authority | Local Owner, remote Viewer/Editor/Owner, stale/malformed provenance, revoked Session/Run, mismatched pairing, and Show page identity cases are covered |
| Vault | Pre-use intent failure causes no handoff; post-use completion failure never replays; one-shot grant cannot be reused; operation-ID reconnect is idempotent and value-free |
| Resource | Report child count/RSS, writer occupancy, queue depth, retry count and host load; distinguish instrumentation from production latency |

Proposed initial targets are relative, not promises: `version/help` should approach interpreter-plus-parser cost with no database access; current-state read commands should have no writer occupancy; warm service enqueue/control p95 should be bounded independently of database history size; and transcript p95 should not regress from the #3280 RC12 result under the same offered workload. The owner should set absolute thresholds after the first post-phase measurement rather than treating the 29.389 s instrumented cold baseline as a service-level target.

---

## 6. Non-goals and known limits

- This plan does not implement a service, change a schema, alter authority, restart Avibe, change Relay refresh, or touch Vault credentials.
- It does not claim that a warm pool makes SQLite writers parallel. SQLite still has one physical writer; the goal is to remove unnecessary writers and bound the necessary ones.
- It does not make arbitrary `vibe data query` SQL a controller endpoint. Its guarded RO connection remains the simpler boundary initially.
- It does not turn `avibe.bot` into a workspace/runtime proxy. Browser traffic and service clients still reach the user's local runtime or managed instance through the intended tunnel/control boundary.
- The benchmark does not cover real avault cryptography, Keychain latency, protected approval UX, arbitrary external HTTP semantics, or the owner's multi-gigabyte database. Those require separately approved, hermetic fixtures.

**Decision point:** approve or reject the target split (lazy entry + readiness probe + RO reads + warm service + Vault contract). Once approved, implementation should start at Phase 1/2 and remain independently reversible; no phase should be inferred as owner approval for the Vault or authority-boundary phases.
