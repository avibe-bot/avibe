# CLI runtime path and state architecture

**Status:** direction approved for personal-agent performance work, 2026-11-06; docs only. Implementation starts in a separate lane after this plan merges.

## Open decisions

1. **Service down:** approve returning "not executed" for service-owned mutations when no handoff occurred, without direct-SQLite fallback or automatic service start.
2. **Phase 2 readiness:** determine whether existing schema/data markers suffice; a new readiness stamp would require a separately approved migration.

**AI Relay automatic refresh stays part of normal behavior.** The temporary panel-refresh pause used during the incident is an operational mitigation only; it must not become the architecture or acceptance benchmark.

## Executive recommendation

Split the current `ensure_sqlite_state()` contract into two explicit operations:

- `probe_current_sqlite_state()`: a bounded read-only check of the exact Alembic revision and required data-migration completion markers. It takes no migration file lock, opens no writable engine, does no backup bookkeeping, and does no report counts. Unknown, missing, malformed, old, or in-progress state falls back to the existing conservative repair path. Whether an additional readiness stamp is needed remains open.
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

1. **No old-schema operation:** prove exact schema/data readiness or complete the conservative repair path before using state; uncertainty never counts as current.
2. **No routine writer from a read:** version/help and ordinary reads do not acquire a migration writer, repair indexes, or prefill Agent models.
3. **No controller-loop blocking:** keep SQLite work, migration locks, subprocess waits and file/network waits off the loop; preserve #2725/#2706 ordering and activation rules.
4. **Behavior parity:** carry the CLI's existing invocation context to the service and apply the same checks. Preserve existing behavior and tests; do not add an identity model.
5. **No hidden fallback:** subject to the service-down decision, return not-executed before handoff rather than silently restoring direct writes or starting a service. A timeout after handoff is not proof of non-execution.
6. **No external replay:** retain today's audit semantics and never automatically replay Vault egress, HTTP requests, or child commands after a possible handoff. Retry only short DB-only transactions where already safe.

### 3.2 Lazy entry and schema readiness

**Entry:** use a small stdlib/parser/version entry, parse before expensive state lookup where today's admission permits it, and lazy-load only the selected command implementation. Preserve existing command checks and their regression tests, including internal activation and dynamic MCP dispatch; this is a startup optimization, not an authorization redesign.

**Readiness:**

- Open existing state read-only (`mode=ro`); never create directories or set WAL mode on this path.
- Read the exact Alembic head and required import/data markers in a bounded query set. Missing tables, malformed markers, unexpected revisions or open failures mean not-ready.
- Prefer existing markers. Phase 2 must demonstrate safe concurrent migration/replacement behavior before enabling the fast path; if those markers cannot express readiness, propose a new stamp with a separately approved migration, rather than assume one exists.
- Keep a consistent read snapshot for direct reads; service-owned work uses the service's readiness lifecycle. Unknown or changing state takes the conservative repair path, never an optimistic continuation.
- Leave backup lifecycle work, history COUNTs, imports and repairs in migration/maintenance. Keep current/old/new/missing/corrupt/replacement/concurrent-upgrade and held-lock tests.

### 3.3 Read-only command layer

- Add explicit RO engine/store variants: no writable bootstrap, index creation or Agent-model prefill during reads.
- `data query` retains its SQL authorizer and limits, but opens ready state directly with `mode=ro`/`query_only`.
- Agent, background, Session and Show list/show handlers use RO state; initialization/repair belongs to the service or explicit maintenance.
- `harness status` merges a bounded persisted snapshot with live IPC and reports controller unavailability without repairing SQLite.
- Retain existing filtering and command checks. Pin bounded query plans for growing tables; RO is not a reason to change which data a command returns.

### 3.4 Warm service/control IPC

Reuse the existing private control channel and warm state engine. Requests carry the operation, arguments and the same invocation context the CLI already receives through injected `AVIBE_*` caller variables. The service applies the same checks the CLI applies today, with no behavior change.

**This caller context is not a security boundary.** Agent shells run as the owner's OS user: they can drop the caller environment and fall back to local Owner, or read `~/.avibe` directly. This performance work neither fixes nor strengthens that model. Keep the existing behavior tests; add no provenance-hardening or role matrix.

Move hot mutations by measured benefit, not by a desire to make every command use IPC:

| Command group | Target path | Service unavailable before handoff (proposed) |
| --- | --- | --- |
| `agent run` (new/session/fork/Bot), Session mutations, Task/Watch mutations, Run cancellation | Service performs the existing durable enqueue/update and live coordination with a warm engine | Not-executed; no direct write fallback |
| `data query`, list/show reads, `harness status` | RO direct reads; retain live enrichment where needed | Standalone reads remain available; report missing live state |
| Vault delivery and its metadata/preparation | Thin client to the existing Vault logic in a warm service; avault retains custody | Not-executed; no automatic service start |
| `skill load/list`, `mcp` | Keep file reads and existing observation/control IPC; optimize import cost | Existing behavior unchanged; do not disable observation |

Keep transactions short and blocking work off the controller loop. Reuse existing scheduling/admission; add queue machinery only if measurements show it is needed. A warm pool removes repeated setup, not SQLite's single-writer limit.

### 3.5 Vault: performance only

- Make `vault fetch` a DB-free thin client to the warm service; use a server-only Show adapter instead of spawning Python for every HTTP call. Reuse today's Vault checks, grant handling and audit semantics unchanged; avault continues to own custody and cryptography.
- Preserve the existing no-replay behavior when an external effect may have occurred, including transport timeout or audit failure. Do not add a new audit protocol or schema.
- Move `vault run` only with transport that preserves caller cwd/environment, stdio, cancellation, signals and exit code without returning decrypted environment values to the client. Reuse existing delivery logic for other Vault commands when profiling justifies moving them.
- Keep #3280/#3281's short transaction/retry foundations. Do not use longer timeouts, a broad global writer or disabled Relay refresh as substitutes for eliminating cold setup.

---

## 4. Phased delivery

Each phase is independently shippable and reversible. #3280/#3281 are landed foundations, not proof that CLI throughput is fixed.

| Phase | Deliverable | Measured reason | Validation / decision |
| --- | --- | --- | --- |
| 1 | Lazy entry/parser and command imports | `vibe.cli` import costs ~0.36–0.43 s in cold profiles | Parser leaf coverage, existing behavior tests, version/help/invalid profiles; revert dispatcher independently |
| 2 | Current-state RO readiness probe; conservative repair fallback | ~0.05 s uncontended bootstrap; 22.465 s p50 in the instrumented 50-process baseline | Current/old/missing/corrupt/concurrent migration and lock cases; approve a migration only if a new stamp is necessary; retain repair fallback |
| 3 | Explicit RO engines/stores; no read-time prefill/index repair; no-bootstrap `data query` | 184 data queries, 123 Harness status calls, 92 Watch lists and 41 Run lists in 27 hours | SQL authorizer, existing filtering tests, bounded query plans and zero read-writer occupancy; revert by handler |
| 4 | Warm-service Agent enqueue, then Watch/Session/Task mutations and associated live cancellation | 363 Agent runs, 100 Watch adds, 35 Session updates and 17 Task adds; repeated admission/bootstrap cost | Approve service-down behavior; test enqueue/order/cancel parity and the 200-call workload; roll back by command family, not automatic fallback |
| 5 | Warm Vault service, DB-free fetch and Show server adapter | Fetch orchestration reached 29.389 s p50 / 48.001 s p95 in the instrumented baseline | Compare cold versus warm fetch/Show adapter latency and process count; retain existing checks/audits and no-replay behavior |
| 6 | Vault `run` transport, then other Vault paths only where profiles justify them | 23 Vault runs in 27 hours; cold `run` profile pays import/admission/bootstrap even before delivery | Process/stdio/cwd/cancel/exit-code parity, audit parity and no replay; benchmark each additional command before expanding scope |

After Phase 3, remeasure before starting the service moves; use remaining call frequency and latency to refine their order. Any further writer/queue work needs measured residual contention, not a speculative phase. Implementation PRs identify changed commands, performance evidence, rollback and any schema or user-visible behavior change requiring approval. Existing identity checks and Vault audit semantics are compatibility constraints, not new design work.

---

## 5. Acceptance benchmark

Use a hermetic synthetic home, test-owned service and loopback stubs: no owner DB, credentials, Keychain, live-service restart or external network. Compare RC12 (including #3280/#3281), each phase and the final warm path.

### Workload

- 200 mixed CLI calls, 20-way concurrency, weighted by the measured command frequencies: Agent enqueue, reads, Watch/Session mutations, skill loads and Vault delivery.
- A simulated turn uses the real `SQLiteTranscriptStore` path at a fixed offered rate; record offered, attempted, committed, retried and skipped writes.
- Measure warm and cold-start service cases with normal AI Relay refresh. A paused panel is a separate mitigation sample, never the acceptance run.
- Use standard Vault fixtures and loopback HTTP/child stubs that count actual handoffs to detect replay. Keep existing behavior tests; add no protected-approval fixture or authority matrix to this performance benchmark.

### Required measurements and gates

| Dimension | Acceptance condition |
| --- | --- |
| Correctness | Zero surfaced SQLite lock/timeout errors, lost/duplicate transcript rows or hidden retry exhaustion in the mixed workload; preserve existing command and audit behavior |
| Latency | Report p50/p95/max for whole process, import, existing admission, readiness/bootstrap, IPC, handler, SQLite wait/commit and external egress separately; do not subtract percentiles |
| Turn safety | Bounded transcript commit latency and controller-loop stalls; no synchronous migration/SQLite wait on the loop |
| Version safety | Current, old, new, missing, corrupt, replacement and concurrent migration states prove no operation uses old/unknown schema |
| Service failure | If approved, unavailable service returns not-executed before handoff, with no direct write fallback or automatic start; post-handoff uncertainty never triggers replay |
| Vault compatibility | Existing grants, checks and audit semantics unchanged; stubs observe no duplicate external requests; `run` preserves process behavior |
| Resource | Report child count/RSS, writer occupancy, existing queue depth, retries and host load; distinguish instrumented from production latency |

Initial targets remain relative: version/help approach interpreter-plus-parser cost without DB access; current-state reads have zero writer occupancy; warm enqueue/control p95 is bounded independently of history size; transcript p95 does not regress from RC12 under the same offered workload. Set absolute thresholds after the first post-phase measurement, not from the 29.389 s instrumented cold baseline.

---

## 6. Non-goals and known limits

- Personal-agent performance only: no enterprise, Organization, member or role-model design, and no identity hardening; preserve existing behavior.
- Intent-first audit is possible future hardening, not part of this work.
- No runtime implementation, deployment, credential change, service restart or Relay refresh change in this PR.
- No arbitrary-SQL controller endpoint, workspace proxy, parallel-writer claim or speculative global queue.
- Measurements do not cover real avault cryptography, Keychain latency, protected approval UX, arbitrary external HTTP semantics or the owner's multi-gigabyte DB.

**Next:** merge this plan, then open a separate implementation lane for Phases 1–3. Only the service-down result and a possible readiness-stamp migration remain open decisions.
