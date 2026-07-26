# Workbench Dispatch Architecture · Design Document

> **Branches**:
> - `feature/workbench-shell` — commit 01..13 (workbench UI + adapters + mirror)
> - `refactor/services-layer` (new) — Plan 1 + Plan 2 implementation
>
> **Status**: Implemented (2026-05-31, PR #351). The sections below are the
> original accepted design; the **Implementation update** immediately following
> records where the shipped code diverged.
>
> **Owner**: cyhhao
>
> **Related show page**: [dispatch-architecture.html](https://alex-app.avibe.bot/p/PjyD9_QC9pU/dispatch-architecture.html) (decision tracker, kept until shipped)

## 0. Implementation update (2026-05-31, PR #351)

What actually shipped differs from the original "C4/C5 streaming" plan in three
material ways. This block is authoritative where it conflicts with the design
sections below.

1. **Streaming model: per-turn SSE → session/page-scoped stream.** The plan
   streamed one turn's reply back over the `POST /api/sessions/<id>/messages?stream=1`
   response (C5) via a streaming `/internal/dispatch` endpoint. Shipped instead:
   the browser loads history once and subscribes to the session's `message.new`
   for as long as the page is open, so *every* message lands live — including
   scheduled-task / watch turns the user never triggered. Send is now
   **fire-and-forget**: `POST .../messages` → `/internal/dispatch_async` (202),
   reply arrives over the stream. A closed tab can't cancel an in-flight turn.

2. **Legacy per-turn dispatch stream retired (Step 6, completed for Show in
   2026-07).** The Chat page's
   `?stream=1` proxy was removed: the ui_server `?stream=1` route branch (the
   Chat page is now fire-and-forget). The turn-sink / `on_chunk` machinery stays
   — `dispatch_async` drives it with a no-op `on_chunk` to keep `in_flight`
   populated (Stop) and to flush the send-while-busy queue on settle.
   Show annotations now use the same `POST /internal/dispatch_async` entry with
   their reserved transcript `message_id`. `SessionTurnManager.submit` decides
   start versus enqueue; replies arrive on the session-scoped `message.new`
   stream. `internal_client.stream_dispatch`, `POST /internal/dispatch`, and
   the unconsumed `show.dispatch` republishing path are removed.

3. **`turn_token` is load-bearing — NOT rolled back.** The plan treated the
   per-turn token as scaffolding. It is now the completion guard: `_stream_chunk`
   and `Controller.mark_turn_complete` only set a sink's `done_event` when the
   emit's `turn_token` matches the sink's, so a late `result` from a stopped /
   timed-out turn can't end the *active* turn (pop `in_flight` / publish
   `turn.end` / flush the queue) while its backend still runs.

### Known trade-offs (intentional)

- **Reused-receiver completion (Claude).** Claude runs one long-lived receiver
  per session, so its emit context carries the *first* turn's token. The result
  path adopts the FIFO-matched pending request's token before emitting, so the
  current turn completes; a turn that fails (auth error) retires its pending
  request so the next turn isn't desynced.
- **No turn-duration timeout (Phase 1a, see `avibe-turn-lifecycle-fsm.md`).** An
  agent turn may legitimately run for hours, so the controller never kills it on
  a timer: `dispatch_turn` holds the stream open with a plain `await done.wait()`
  until the agent's REAL terminal result. A genuinely wedged turn blocks only its
  own session until the user's Stop ends it or a restart resets stale state. Plain
  Stop is no-flush.

### Provenance + queue (added beyond the original plan)

- Every `messages` row carries **`source`** (`user` / `agent` / `harness`),
  distinct from author role, plus `author_name` / `author_id`. Harness turns
  render a Scheduled-task / Watch badge.
- Send-while-busy **queue** + cross-device **draft** reuse the `messages` table
  (no new queue table). A transcript reservation is hidden as `pending`; the
  unified entry either starts the turn so its caller renders the row as `user`,
  or moves it to `queued` for the shared drain. For Show events, dispatch
  lifecycle is stored independently on the stable `show_session_events` row as
  `none` / `in_flight` / `accepted` / `failed`, so rendering changes and queue
  replacement cannot erase acceptance or make a retry ambiguous. Stop keeps the
  queue; "立即发送" interrupts + flushes.

## 1. Why This Document Exists

The workbench UI rewrite (commits 01-13 on `feature/workbench-shell`) added an Agent-Workbench mode
that reads from the shared SQLite store. The next step is **send/compose** — letting the user type
a message in the web Chat surface and trigger a real agent turn.

Implementing this surfaces two structural problems that have been quietly accumulating:

1. **Data-control duplication** — UI server and CLI both directly import `storage/*_service.py`
   modules. For the `agent_sessions` table specifically, two wrappers exist with diverging APIs
   (`storage/workbench_sessions_service.py` for the UI, `storage/sessions_service.py::SQLiteSessionsService`
   for IM/CLI). Same table, two business APIs. Every new feature that touches sessions has to
   pick a side.

2. **No cross-process trigger from Web UI to Controller** — UI server and Controller run as
   separate subprocesses (per `vibe/runtime.py::spawn_background`). When the user clicks "send"
   in the web Chat surface, there is no path for the UI server process to ask the Controller
   process to run an agent turn. IM adapters (Slack/Discord/Telegram/Lark/WeChat) are in-process
   with Controller and call `message_handler.handle_user_message` directly. CLI uses the
   `agent_runs` queue (async, ~2s scheduler poll, batch JSON output).

These are two independent concerns but they collapse onto **one shared abstraction**: a
`dispatch_turn()` entry point that all three callers (IM adapters, CLI, Web UI) converge on.
Solve them together and the architecture stays clean. Solve only one and the other festers.

## 2. Goal

When this design lands:

- A user can compose a message in the web Chat surface, hit send, and see the agent's reply
  stream in token-by-token (or chunk-by-chunk) — comparable latency to talking to the bot from
  Slack/Discord.
- IM adapters, CLI, and Web UI all funnel through a single `dispatch_turn()` function that
  encapsulates the agent-turn lifecycle. Behavior changes there propagate to all three callers
  without copy-paste.
- The UI server process never imports `storage/sessions_service.py` directly — it goes through
  `core/services/sessions.py`. Same for the CLI. Contract tests pin the shape so the two
  callers cannot drift again.
- `~/.vibe_remote/state/dispatch.sock` exists at runtime and exposes a minimum set of internal
  RPC endpoints over which the UI server can trigger Controller-side runtime side-effects
  (turn dispatch, cancel). The socket is `0o600` and local-only.
- Pure data CRUD (list projects, update session title, mark read) **does not** go through the
  socket. The boundary rules (§7) are enforced by code review.

## 3. Non-Goals (Explicit)

- **Process merge (combining UI server + Controller into one process)** is out of scope and
  rejected. The two-process model is intentional (separate reload lifecycles) and changing it
  would also conflict with the IM thread / uvicorn worker model.
- **WebSocket** is rejected in favor of SSE chunked over Unix socket. Rationale: SSE rides on
  plain HTTP, auto-reconnects via `EventSource`, and matches the existing `/api/events` pattern
  in `vibe/sse_broker.py`. Cloudflare Tunnel research from 2026-05-24 also favored SSE.
- **Polling-only mode for send/compose** (the original "enqueue `agent_runs`" approach) is
  rejected as the primary path. ~2s scheduler delay and no streaming would lock us out of the
  streaming UX. The queue path stays as a fallback when the socket is unreachable.
- **gRPC, JSON-RPC, or any structured RPC framework** is out of scope. RESTful endpoints on a
  Unix socket suffice for ~5-10 RPC verbs.

## 4. Current State (Post-Master 2026-05-26)

Authoritative inventory. Verified against the current `feature/workbench-shell` head.

### 4.1 Data domains and their access paths

| Domain | Underlying table | Storage layer (CRUD) | Business layer (UI / CLI / IM) | Status |
| --- | --- | --- | --- | --- |
| Agents | `vibe_agents` | `core/vibe_agents.py::VibeAgentStore` | shared across UI + CLI | **Unified** |
| Sessions | `agent_sessions` | SQLAlchemy table | UI: `workbench_sessions_service`; IM/CLI: `SQLiteSessionsService` | **Split (target of Plan 1)** |
| Messages | `messages` | `messages_service` | UI + commit 13 mirror both go through service | **Unified** |
| Projects | `scopes` + `scope_settings` | `projects_service` | UI only | **Unified** |
| Runs / Tasks queue | `agent_runs` + `run_definitions` | `storage/background.py::SQLiteBackgroundTaskStore` | `core/scheduled_tasks.py::TaskExecutionStore` delegates to storage layer | **Properly layered** |
| Watches | `watches` (runtime records) | `core/watches.py` | CLI writes, UI reads via background store | **Partial** |
| Settings / Config | JSON file + `scope_settings` | `settings_service` | UI reads `config.json` directly; CLI uses `SettingsStore` | **Split (Plan 1 phase 2 target)** |
| Auth codes | `auth_codes` | `settings_service` | shared | **Unified** |

The actual duplication is **only Sessions and Settings**. Everything else is either already
unified or layered correctly. This was misjudged in the v1 exploration of this design;
verified again on 2026-05-26.

### 4.2 Cross-process facts

- **UI server** runs as a subprocess started by `vibe/runtime.py::spawn_background` (line 835).
- **Controller** runs in a separate process (`main.py:165 controller.run()`), holding the IM
  thread and the agent-runtime asyncio loop.
- **SSE broker** (`vibe/sse_broker.py`) is **in-process** — UI server and Controller each have
  their own `SSEBroker()` singleton, not shared.
- **`agent_runs` queue** is the only existing cross-process trigger path. It is consumed by
  `core/scheduled_tasks.py::ScheduledTaskService._drain_requests` on a ~2s poll. Already
  supports `request_type="agent_run"` which routes to `message_handler.handle_scheduled_message`.
- **Process merge is documented as forbidden** in `docs/CLI.md:262-276` ("Vibe Remote manages
  two types of processes" with independent reload lifecycles).

## 5. Architecture (Target State)

```
  +-------------------+     +-------------------+
  |  Web UI (Chrome)  |     |  vibe agent run   |
  +---------+---------+     +---------+---------+
            |                         |
            | POST + EventSource      | --async  --sync
            v                         |   (queue)   (socket)
  +-------------------+               |     |          |
  |  UI server proc   |               |     v          v
  |  - SSE proxy      |   <----- HTTP+SSE chunked  --->|
  |  - /api/*         |   over ~/.vibe_remote/state/dispatch.sock
  |  - core/services  |               |                |
  +---------+---------+               |                |
            |                         |                |
            |   (data layer)          |                |
            v                         v                v
  +------------------------------------------------------------+
  |               core/services/                               |
  |   sessions.py  messages.py  dispatch.py  ...               |
  |               (shared business API)                        |
  +------------------------------------------------------------+
            |                         |                |
            v                         v                v
  +------------------------------------------------------------+
  |               storage/  (SQLAlchemy CRUD)                  |
  |    + core/internal_server.py  (Controller-only unix sock)  |
  +------------------------------------------------------------+
                                      |
                                      |  IM adapters (same process as Controller)
                                      |  call dispatch_turn() directly (no socket)
                                      v
                              +---------------+
                              |  Controller   |
                              |  + IM thread  |
                              |  + asyncio    |
                              |  + scheduled  |
                              +---------------+
```

Key insight: `core/services/dispatch.py::dispatch_turn(context, text, *, on_chunk=None)` is
the single shared entry. Three callers reach it differently:

- **IM adapter** (Slack, Discord, …): same Python process as Controller → `await dispatch_turn(...)`
  directly. Zero IPC overhead. Unchanged from today's behavior; the entry point just moves
  into `core/services/`.
- **Web UI**: separate process → `httpx.stream("POST", "http+unix://.../internal/dispatch", json=...)`
  → Controller's `core/internal_server.py` calls `dispatch_turn(...)` and yields SSE chunks
  back over the same socket. UI server proxies the chunks to the browser's `EventSource`.
- **CLI** (`vibe agent run --sync`): also through the socket. `--async` keeps using the
  `agent_runs` queue for cron/hook use cases that should not block.

## 6. Plan 1 · Data Control Layer

### 6.1 Target

A new package `core/services/` holds business-level APIs that take a SQLAlchemy `Connection`
(or transaction context) and never own engines or process-level state. UI server, CLI, and
Controller all import from there. Storage layer (`storage/`) stays as pure CRUD.

### 6.2 Scope

- **In scope (Phase 1A)**:
  - `core/services/sessions.py` — merges `workbench_sessions_service` + `SQLiteSessionsService`.
  - `core/services/dispatch.py` — extracts `dispatch_turn()` from
    `message_handler._handle_turn`. This is also Plan 2's dependency.
- **In scope (Phase 1B)**:
  - `core/services/settings.py` — unifies CLI `SettingsStore` ↔ UI direct `config.json` reads.
- **Out of scope (already unified)**:
  - Agents, Messages, Projects, Runs — no work needed.
- **Out of scope (later phase)**:
  - Watches, vaults, skills — UI hasn't fully wired them, defer until UX direction settles.

### 6.3 Service API conventions

- All public functions take `conn: Connection` as their first argument. Never construct
  engines inside the service.
- Return shapes are plain `dict[str, Any]` matching the existing `_row_to_payload` style in
  `messages_service`. No ORM mappers leaking out.
- Errors raise `LookupError`, `ValueError`, or domain-specific exceptions defined in
  `core/services/errors.py`. Routes / CLI translate them to HTTP status / exit codes.
- No side effects (SSE publish, log writes) inside service functions. Side effects belong in
  the calling layer (routes for UI, controller for IM, CLI for terminal output).

### 6.4 Commit breakdown

- **C1** · Extract `core/services/sessions.py` + `core/services/dispatch.py` + contract tests.
  IM adapter and UI server change `import` paths; CLI unchanged.
- **C2** · Migrate CLI `_session_service()` to `core/services/sessions.py`. Verify
  `vibe agent run --json` output schema is byte-identical (pinned in test).
- **C3** · Extract `core/services/settings.py`. UI server direct `config.json` reads and CLI
  `SettingsStore` both go through it.

### 6.5 Risks

- CLI `--json` schema stability (decision Q8: strict). Mitigated by snapshot test in C2.
- `SQLiteSessionsService` carries a lot of legacy semantics (session reservation, anchor
  resolution). These need to move with the API; some are IM-only and should remain hidden
  behind helper functions inside `sessions.py`, not exposed as the public surface.

## 7. Plan 2 · Cross-Process Dispatch (N3)

### 7.1 Target

Controller process binds a Unix socket at `~/.vibe_remote/state/dispatch.sock` and runs a
minimal FastAPI app on it. UI server uses `httpx.AsyncClient(transport=AsyncHTTPTransport(uds=...))`
to stream RPC calls over the socket. SSE chunked responses flow turn output back to UI server,
which proxies it to the browser's `EventSource`.

### 7.2 Why N3 over alternatives

| Candidate | Decision | Reason |
| --- | --- | --- |
| N1 · `agent_runs` queue + polling | Fallback only | ~2s scheduler delay, no streaming. Future streaming would require recreating this path. |
| N2 · Process merge | Rejected | `docs/CLI.md` explicitly enforces separate processes. Also conflicts with IM thread / uvicorn worker model. |
| N3 · Unix socket SSE | **Selected** | ~5ms trigger latency, native streaming via SSE chunked, shares `dispatch_turn()` with IM and CLI, doesn't break process boundaries. |

### 7.3 Component changes

- **New** · `core/internal_server.py` — FastAPI app bound to a unix domain socket. Exposes:
  - `POST /internal/dispatch` (body: session_id, text, scope_id, agent_overrides) → SSE
    chunked response. Each `emit_agent_message` chunk produced by `dispatch_turn` is yielded
    as `data: {...}\n\n`.
  - `POST /internal/cancel/<run_id>` → marks the in-flight turn for cancellation, returns
    when the cancel hook fires.
- **New** · `vibe/internal_client.py` — `httpx` wrapper for UI server. Handles connection
  setup, reconnect on transient drops, and SSE parsing into `AsyncIterator[dict]`.
- **Modified** · `core/controller.py` — `controller.run()` starts the internal server as a
  background asyncio task. Same event loop, no separate thread.
- **Modified** · `vibe/ui_server.py` — `POST /api/sessions/<id>/messages?stream=1` becomes a
  `StreamingResponse(text/event-stream)` that streams from the internal client.
- **Modified** · `core/message_dispatcher.py::emit_agent_message` — adds an optional
  `on_chunk` callback that `dispatch_turn` passes in. When present, every emit also feeds
  `on_chunk(envelope)`. When absent, behavior is unchanged for IM adapters (which don't need
  the stream-out side channel).
- **Modified** · `ui/src/components/workbench/ChatPage.tsx` — adds compose textarea, send
  button, Cmd/Ctrl+Enter binding, optimistic user-message append, and a `fetch`+`ReadableStream`
  consumer that appends agent chunks to the transcript as they arrive.

### 7.4 Endpoint catalog (v1 + v2)

| Endpoint | Type | Purpose | Phase |
| --- | --- | --- | --- |
| `POST /internal/dispatch` | B | Trigger a turn, SSE chunked response | **v1** |
| `POST /internal/cancel/<run_id>` | B | Cancel in-flight turn | **v1** |
| `POST /internal/platform/<name>/start\|stop` | B | Start / stop an IM platform adapter | v2 |
| `POST /internal/backend/<name>/restart` | B | Restart agent backend (replaces `RuntimeCommandWatcher` markers) | v2 |
| `POST /internal/config/reload` | B | Controller reloads `V2Config` | v2 |
| `GET /internal/events` | C | Long-lived SSE: Controller pushes non-request-driven events to UI server | v2 |

v1 is the smallest set that unblocks send/compose. v2 work doesn't block v1 release.

### 7.5 Commit breakdown

- **C4** · `core/internal_server.py` + `vibe/internal_client.py` + dispatch endpoint. Tests
  cover socket up/down, basic dispatch round-trip, and SSE chunked framing.
- **C5** · UI server POST route emits via internal client; ChatPage compose UI; en/zh i18n;
  fallback to N1 queue when socket is unreachable.
- **C6** · `POST /internal/cancel` endpoint + UI "stop" button on running turn.

### 7.6 Boundary rules (R1 / R2 / R3)

These are enforced by code review. PR descriptions must state which rule each new endpoint
or function falls under.

- **R1** · **Type A (pure data CRUD) never goes through the socket.** If the operation can
  be completed in the UI server process by reading/writing SQLite, it must use
  `core/services/*` directly. Examples: list projects, create session, update session title,
  mark messages read.
- **R2** · **Type B (Controller runtime side effects) uniformly goes through N3 socket.**
  Examples: trigger a turn, cancel a turn, restart a backend, start/stop an IM platform,
  reload config. One transport, one mental model.
- **R3** · **Type C (Controller → UI async push) goes through SSE.** First version: only
  in-call SSE chunked response (already covered by N3 `dispatch`). Future: a long-lived
  `GET /internal/events` SSE if non-request-triggered pushes are needed.

### 7.7 Anti-examples (must not go through socket)

- Listing projects / sessions / messages / agents — pure queries.
- Updating session title / model / effort — pure UPDATE on `agent_sessions`.
- Marking messages read — pure UPDATE.
- Listing watches / logs / runs — pure queries.

If a Type-A operation is found going through the socket during code review, that PR is
blocked until it's moved to `core/services/`.

### 7.8 Failure modes

- **Socket unavailable on UI server startup** — UI server logs a warning and falls back to
  N1 queue mode for `dispatch`. Cancel returns 503. UI surfaces "high-latency mode" badge.
- **Controller crashes mid-turn** — Internal server shutdown triggers cleanup of pending
  responses; UI server `httpx.stream` raises, caught, surfaced to ChatPage as
  "agent disconnected". User can retry; turn state in DB is left in `running` (next
  controller boot reclaims via existing `recover_processing_runs`).
- **UI server restarts mid-turn** — Browser `EventSource` auto-reconnects to the new UI
  server; in-flight messages on the Controller side complete and write to messages table;
  the reconnected browser refetches the transcript and shows the now-completed agent reply.
- **Socket file permissions wrong** — Refuse to start UI server with explicit error pointing
  to the socket path.

## 8. Resolved Decisions (from Q1..Q10)

| # | Question | Decision |
| --- | --- | --- |
| Q1 | Cross-process candidate | **N3** Unix Socket SSE |
| Q2 | Plan 1 / Plan 2 order | Co-implemented (Plan 1 extracts `dispatch_turn`, Plan 2 uses it) |
| Q3 | Service layer location | `core/services/` |
| Q4 | First domain to slice | Sessions + dispatch_turn together |
| Q5 | Socket path | `~/.vibe_remote/state/dispatch.sock` with `0o600` perms |
| Q6 | Fallback path | UI server falls back to N1 queue when socket unreachable |
| Q7 | session_id resolution | UI server auto-creates avibe session when missing |
| Q8 | CLI schema stability | Strict — `vibe agent run --json` schema unchanged, pinned in test |
| Q9 | Existing 13 commits | Open PR now into Codex review; new work on a separate branch |
| Q10 | Refactor branch | New `refactor/services-layer` worktree |

## 9. Execution Order

1. **PR-A** — Open PR from `feature/workbench-shell` (commits 01-13 + this document = 14 commits)
   into `master` for Codex review. Keep the review loop running via `background-watch-hook`.
2. **Worktree setup** — Create `~/vibe-remote-project/.worktrees/avibe/refactor/services-layer/`
   from latest master once PR-A merges (or branch from `feature/workbench-shell` if we want to
   start before merge — to be decided based on PR-A timing).
3. **C1** — Extract `core/services/sessions.py` + `core/services/dispatch.py` + contract
   tests. IM adapter `import` change. No behavior change. Open PR-B for Codex review.
4. **C2** — Migrate CLI `_session_service()` to the new service. `vibe agent run --json`
   schema snapshot test. PR-C.
5. **C3** — `core/services/settings.py`. PR-D.
6. **C4** — `core/internal_server.py` + `vibe/internal_client.py` + `POST /internal/dispatch`.
   PR-E.
7. **C5** — UI server `POST /api/sessions/<id>/messages?stream=1` + ChatPage compose UI +
   fallback to N1. PR-F.
8. **C6** — `POST /internal/cancel` + UI stop button. PR-G.

Each PR is independently reviewable. The chain of dependencies is C1 → (C2, C4) → C5 → C6.
C3 can land anywhere after C1.

## 10. Test Strategy

- **Contract tests** in `tests/contract/test_services_*.py` — assert that the same business
  call (e.g. `services.sessions.create()`) produces identical row shape regardless of caller.
  Pin field names, types, default values.
- **Snapshot tests** for `vibe agent run --json` output. Test diffs on schema change.
- **Integration test** for the socket round-trip: spawn a fake controller, send a dispatch,
  assert SSE chunks arrive in order and the transcript matches.
- **Existing tests** (`test_message_dispatcher_*`, `test_message_handler_*`, etc.) must
  continue to pass without modification — IM adapter behavior is the regression backstop.
- **No new test framework** introduced. Pytest only.

## 11. Open Items (Not Blocking)

- Whether `vibe agent run --sync` should become the default once the socket path is
  reliable. Defer to a separate UX discussion.
- Whether the long-lived `GET /internal/events` SSE for Controller→UI push (v2) should
  replace `vibe/sse_broker.py` entirely or complement it. Defer to v2.
- Cancellation semantics: should a cancelled run leave a `cancelled` row in `agent_runs` or
  delete the row? Current `storage/background.py` supports `cancelled` status; reuse it.

## 12. Out of Scope, Recorded for Future

- gRPC / Protobuf — would force schema versioning overhead. Reconsider if endpoint count
  exceeds ~15.
- Replacing `RuntimeCommandWatcher` (`core/runtime_commands.py`) entirely with the new
  internal endpoints. Listed as v2 work above; not blocking v1.
- Web UI sending messages **to other IM platforms** (e.g. Web UI → Slack channel) — this is
  a product decision, not a transport decision; current scope keeps web Chat strictly
  avibe-platform.
