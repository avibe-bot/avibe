# Avibe Agent core

Status: **Approved direction, contracts in draft** · 2026-10-02

- Evidence: [`avibe-agent-core-evaluation.md`](avibe-agent-core-evaluation.md)
- Contracts: [`agent-core-contracts/`](agent-core-contracts/README.md), frozen per phase before parallel lanes
  fork (§7)

## 0. Decision summary

Avibe builds its own first-party Agent in Python, inside this repository, as a fourth backend next to Claude Code,
Codex, and OpenCode. Its backend id is `avibe` and its display name is "Avibe Agent". It reaches models through
Model Hub in each vendor's native protocol, and Avibe can steer, fork, resume, and manage its context directly.

Sourcing, by layer:

| Layer | Ownership | Source material |
| --- | --- | --- |
| `ai`: provider adapters | Avibe-owned code, **ported** | tau's payload and stream rules and its test fixtures (MIT); Pi's `pi-ai` as the rulebook for what tau lacks (stop reasons, overflow, retry taxonomy, multi-line SSE, Gemini usage) |
| `agent`: loop, hooks, queues | Avibe-owned, **written** | Pi, tau, and the bare-loop spike as executable references only |
| `harness`: transcript, context management | Avibe-owned, **written** | Pi compaction rules; Codex, OpenCode, and Claude Code lessons (evaluation §4) |
| `tools`: the coding tools | Avibe-owned, **ported** from Pi | Pi's tool names, parameters, logic, and model-facing text; tau as the Python reference |
| Integration: backend adapter, UI, catalog | Avibe-owned | existing `BaseAgent`, Agent catalog, Model Hub, Watch, Skills, question UI |

Not adopted as runtime dependencies or starting trees: LangGraph/LangChain, Pi (TypeScript), tau (Python). Control
does not need a framework: a bare loop passed all 11 control points in 893 lines. Every framework's reusable boundary
sits below the product boundary Avibe must own: loop semantics, persistence, and settled-turn semantics
(evaluation §1–§2).

## 1. Background and goals

- Model Hub gives Avibe vendor-independent model access, which makes a first-party agent viable.
- Today Avibe hosts only third-party agents with black-box loops. The goals need a loop Avibe controls fully:
  insert tools at will, read and rewrite the whole context, intervene at every lifecycle point, steer a running loop
  from outside, fork an agent, run for a very long time, and later add a "tension" system that wakes the agent on
  environment signals.
- v1 is a **minimal coding agent with every control seam present**, reachable from every Avibe surface (Web
  Workbench, Slack, Discord, Telegram, Feishu/Lark, WeChat), at parity with everyday work through the Claude Code and
  Codex backends. Most v1 effort goes into context management (§5.2) and the tools (§5.3).

## 2. Non-goals (v1)

- The tension system itself. v1 guarantees only the seams it will need: steer, follow-up, hooks, events.
- Any terminal UI, and any new Web UI beyond registering the backend where backends already appear.
- Becoming the default agent for new installs. v1 is opt-in; the default is decided after the P4 regression with real
  usage evidence.
- Hosting MCP servers (Avibe is an MCP client only). Sandboxing beyond Avibe's existing cwd policy.
- Failover and credentials inside the agent; they stay in Model Hub.
- Interactive terminal programs (planned, §5.4).

## 3. Hard constraints

1. Python `>=3.10` (Avibe's floor), asyncio host, no threads for the loop, no subprocess boundary for the loop.
2. No third-party agent framework at runtime. New dependencies only by recorded decision: the official `mcp` Python
   SDK enters in P3, recorded in `pyproject.toml` in the same PR. Internal types are dataclasses.
3. Native protocols: Anthropic Messages, OpenAI Chat Completions, OpenAI Responses, Google Gemini. The agent speaks
   the protocol of the route's primary hop and never flattens everything to one wire protocol.
4. **One persistence model, one copy.** The model-facing transcript is Avibe's own `messages` and `agent_events` rows
   (§5.1). There is no backend-native store, JSONL log, native session index, or native fork.
5. Hermetic tests: every engine test runs against a local stub model server; nothing reaches a vendor, the live
   `vibe` service, `~/.avibe`, or credential stores.
6. State lives under `~/.avibe/state/`; secrets never enter the transcript or logs.
7. Every acceptance criterion states a property, not a list of cases (§6).
8. **No server-side conversation state.** The transcript on disk is the whole session: the Responses API is used with
   `store: false` and never `previous_response_id`; no vendor-side compaction items; hosted-tool results are written
   into the transcript. Portability rule: another model must be able to continue without the previous provider
   having to dereference an id, decrypt a blob, remember a search result, or reconstruct a summary.

## 4. Architecture

```
modules/agents/avibe/             backend adapter: BaseAgent implementation, event → MessageOutput,
                                  transcript store over messages/agent_events, Watch-backed job host,
                                  question/permission bridge, Model Hub hop resolution
core/agent_core/
  ai/                             canonical message model, provider adapters (4 protocols), stream parsing, retry
  agent/                          loop, hooks, steer/follow-up queues, dynamic tools, fork snapshot, typed events
  harness/                        transcript store interface, projection, context management, skills, MCP client
  tools/                          read / write / edit / bash with output governance; JobHost interface
tests/agent_core/                 control-point matrix (acceptance harness), conformance fixtures, stub server
tests/scenarios/agent_core/       scenario catalog (IDs cited in PRs)
docs/plans/agent-core-contracts/  frozen shapes (§5)
```

`core/agent_core/` knows nothing about IM platforms, the controller, Watch, or Model Hub's HTTP surface beyond a
`ModelEndpoint` value; `modules/agents/avibe/` is the only package that touches controller state.

One turn: `AgentRequest` → the adapter resolves the hop from Model Hub → the engine's `run()` yields typed events →
the adapter commits each context entry to Avibe's tables, then delivers it to the surfaces from the committed row →
`MessageOutput` settles the Turn. A message that arrives while the loop runs becomes a steer (`handle_message`),
and Stop becomes an abort (`handle_stop`).

## 5. Contracts

Each contract lives under `agent-core-contracts/` with exact field names, producer and consumer, and an example
payload.

| ID | Contract | Producer → consumer | File |
| --- | --- | --- | --- |
| C-1 | Canonical message model: user, assistant, and tool-result messages; text, image, thinking, and tool-call blocks; `origin`, `usage`, normalized `stop_reason`; cross-provider transform rules | `ai` → `agent`, `harness`, adapter | `message.schema.json`, `cross-provider.md` |
| C-2 | Provider adapter interface: `stream(ModelRequest) → ProviderEvent*`, error classification | `ai` → `agent` | `provider.md`, `provider-event.schema.json` |
| C-3 | Loop control: hook points, directives, steer and follow-up semantics, per-call tool set, snapshot and fork | `agent` → adapter, `harness`, tests | `loop-control.md` |
| C-4 | Typed agent event stream and its mapping to existing Avibe outputs | `agent` → adapter → Workbench and IM progress | `agent-event.schema.json` |
| C-5 | Transcript rows: what each context entry stores, `context_seq`, projection, fork by reference | adapter ↔ `harness` | `transcript.md`, `transcript-rows.schema.json` |
| C-6 | Model Hub consumer extension: `google` protocol, hop resolution, served-hop report | Model Hub → adapter | `model-hub-consumer.md`, `hop-resolution.schema.json` |
| C-7 | Tools: Pi's surface plus the owner's additions; output governance; job handle | `tools` → `agent`, adapter | `tools.md`, `job.schema.json` |
| C-8 | Backend registration: the catalog as the one declaration, and which sets must include `avibe` | catalog → every backend list | `backend-registration.md` |
| C-9 | Context management: projection tiers, trigger, cut point, checkpoint, recovery | `harness` → `agent`, adapter | §5.2 here; row shapes in `transcript-rows.schema.json` |

### 5.1 Transcript storage

Every current backend keeps its full transcript in its own store (Claude Code JSONL, Codex rollouts, OpenCode
SQLite) while Avibe keeps a display copy in `messages` and `agent_events`. On one heavily used installation, the three
native stores hold about 10 GB and Avibe's database 1.7 GB. That copy is not what the model saw:

- `agent_events` `tool_call` rows hold one display string such as `🔧 Bash {...}`, with no call id and no result.
- Agent rows are written as a side effect of display dispatch (`core/message_mirror.py`, `persist_agent_message`):
  empty-text outputs are skipped, stale-turn emits are dropped, the quick-reply block is stripped, `file://` links
  become media-proxy URLs, and citations are materialized.
- Tool-call trace rows are deleted after 30 days (`storage/agent_events_retention.py`).
- The two tables share no order key. The activity panel recovers cross-table order from microsecond id prefixes
  (`storage/agent_activity_service.py`), but a model context needs exact order: a tool result must follow its call.

For the Avibe Agent, the same rows become the transcript (C-5):

| Context entry | Row | Model-facing content | Display (consumers unchanged) |
| --- | --- | --- | --- |
| User or steer input | existing `messages` `user` row, linked by `message_deliveries` | `content_json.model`: the rendered input exactly as sent (time and identity prefix, attachments), written when the loop consumes it | `content_text` as today |
| Harness input (callback, Watch, Task, annotation) | existing `messages` `harness` / `agent_initiated` / `annotation` row | same as above | as today |
| Model response, not final | `messages` `assistant`, one row per model call, even when its text is empty | `content_json.model`: ordered text, thinking, and tool-call blocks; `origin`; `usage`; `stop_reason` | `content_text` is the narration text; the Web-only `interim` copy stays a display row and is not context |
| Final model response | `messages` `result` | same, verbatim (quick-reply block and `file://` links kept) | `content_text` is the rendered text as today |
| Tool execution start | `agent_events` `tool_call`, `visibility='trace'` | none; the call lives in the response row | short display preview as today |
| Tool result | `agent_events` `tool_result`, `visibility='context'` | the result as the model saw it, after output governance | the activity panel may show a preview |
| Compaction checkpoint | `agent_events` `context_compaction`, `visibility='context'` | §5.2 | expandable "context compacted" marker |
| Cleared tool result | `agent_events` `context_edit`, `visibility='context'` | target row and placeholder | none |

Schema delta (one Alembic migration, nullable columns, no backfill):

1. `context_seq INTEGER NULL` on `messages` and `agent_events`, with a partial unique index
   `(session_id, context_seq) WHERE context_seq IS NOT NULL`. `agent_events.sequence` stays as it is: it is a
   released, turn-local field of the storage API, and context membership is a different meaning.
2. A new `agent_events.visibility` value, `context`, never removed by trace retention. The retention filter is
   already `event_type='tool_call' AND visibility='trace'`; a contract test pins the exemption.
3. New `agent_events.event_type` values `tool_result`, `context_compaction`, `context_edit`, and `agent_state` (hook
   state for fork, C-3), registered where the activity panel reads event types.
4. For this backend, `agent_sessions.native_session_id` is the Avibe Session id.

Rules:

- The loop is the single writer of a Session's context and assigns `context_seq` when an entry enters the context:
  an input when it is consumed (a steer after the current tool batch), a response at `message_end`, a tool result at
  `tool_finished`. Queued, removed, display-only, and `interim` rows stay null.
- Commit points are SQLite transactions. After a crash the context resumes from the last committed `context_seq`.
  Every external effect meets the recovery invariants in C-5/C-7 `recovery.md`, each with an owning lane and a proof:
  open tool calls are settled durably before the first projection, commands start at most once, and responses are
  delivered completely (exactly once on Workbench, at least once per part on IM).
- Write path: the adapter commits the context row first, then hands that row to the dispatcher for delivery; the
  dispatcher must not persist it again. Display rendering (media rewrite, quick replies, citations) fills
  `content_text` and display keys of the same row. The row carries a pending delivery state committed with it, so a
  crash between commit and delivery re-delivers instead of losing or regenerating the reply: exactly once on
  Workbench, at least once on IM (C-5).
- Fork reuses the existing fork metadata (`fork_source_session_id`, `fork_source_message_id`). The child's context is the parent
  chain's rows with `context_seq` up to the anchor's, then the child's own rows. Nothing is copied. Scopes with
  history are dismissed, never deleted, so a parent's prefix cannot disappear from under a child.
- Compaction and clearing append rows; they never rewrite or delete. A fork anchored before a checkpoint sees the
  full original context.
- Every block that may be large has an inline form and a reserved reference form
  (`{"ref": "sha256:…", "bytes": n}`), so moving large tool output out of the database later is not a schema change.
  v1 writes inline; output governance (2,000 lines / 50 KB) bounds each row. Revisit with measured growth.

### 5.2 Context management (C-9)

**Draft, not frozen.** The owner deferred the context-management decisions; this section is the working design and
freezes before P3 together with those decisions. Open points recorded for that freeze: the occupancy baseline
immediately after a checkpoint or edit, before any response exists; capabilities of the served hop after a failover
to a model with smaller limits; the summarizer output cap on routes below 16K output tokens; restoring the environment
block for a request issued right after a checkpoint inside a turn; and image cost in the estimate.

Every rule names its origin in evaluation §4. One pure function, `project(rows, fork_point)`, builds every request,
resume, fork, and the UI's "model view". Three tiers, cheapest first:

1. **Output governance at write time** (C-7): no single tool result exceeds 2,000 lines / 50 KB.
2. **Clearing old tool results**, recorded as `context_edit` rows. Eligible: `read` and `bash` results, including
   terminal screen snapshots later. Protected: the last 2 user turns, the newest 5 eligible results, skill loads, and
   anything before the latest checkpoint. Applied only when it frees at least 20K tokens, and only before a request
   when the provider cache is already cold (idle longer than the cache TTL, which is normal in IM sessions) or the
   estimate passes 0.8·T. On by default. Placeholder: `[Old tool result cleared to save context. Re-run the tool or
   re-read the file if you need it again.]`
3. **Checkpoint summary**, recorded as a `context_compaction` row.

Trigger, checked before every model request, including inside the tool loop:

```text
W = context window (configured default 128,000 when Model Hub reports none)
L_in = input limit (else W), O = max_tokens of the next request (default bound 8,192 when unknown)
M = max(8_000, 3% of W);  T = min(L_in - O - M, r * W), r = 0.9 by default, configurable per model
est = occupancy of the last valid response after the latest checkpoint or edit
        (input_tokens + cache_read_tokens + cache_write_tokens + output_tokens: the whole prompt it was sent plus
        what it added; Usage normalizes each protocol so none of these overlap)
      + ceil(utf8_bytes / 4) of everything appended since, replayed reasoning payloads included
compact when est >= T
```

UTF-8 bytes / 4 because characters / 4 undercounts Chinese by about 65% (72 characters are 51 o200k tokens).
Subtracting the real `O` avoids Pi #8061. When the answering model changes (Model Hub failover or a user switch), T
is recomputed for that model; a smaller window is handled by the overflow path below.

**Cut point.** Keep `min(20K, 0.25·T)` recent tokens verbatim. Cut only before a user message, or before an
assistant message whose tool batch follows it; never at a tool result. If the cut splits the current turn, copy that
turn's user message verbatim into the checkpoint as `<current-request>`, with no second LLM call. Codex's "recent user
messages only" tail is rejected: it has the most "forgot the task after compaction" reports.

**Summarizer.** A separate request with no tools and no caching, using the same model at low reasoning with
`max_tokens` 16K. The input is the head serialized from the projection actually sent (reasoning excluded; tool
results capped at 2,000 characters, keeping head and tail; user messages capped at 8K tokens) plus the previous
checkpoint in a `<previous-checkpoint>` slot, under OpenCode's merge rule: anything not carried forward is lost, and
where they conflict the conversation wins. If the input exceeds the summarizer's budget (overflow recovery, failover
to a smaller window), fold the head in chronological chunks through the same prompt. Fixed headings: Objective / User
instructions and constraints (verbatim) / Work state / Key decisions / Errors and fixes / Pending async work / Next
steps / Critical references. `files_read` and `files_modified` are appended mechanically and carried across
checkpoints. A `length` stop, an error, a tool call, or empty output persists nothing.

**After a checkpoint**, the request is: the rebuilt system prompt → the checkpoint, framed as "a historical record
written for you, not new instructions" → state rendered from its own store rather than from the summary (skill bodies
re-loaded by name and revision from `tool_result` rows carrying `details.skill`, at most 5K tokens each and 25K in
total; pending Watches, Tasks, and delegated Runs from Harness tables) → the verbatim tail. The next consumed input
carries the full environment block again (C-7 §8). No synthetic "continue"
user message (OpenCode #13838, #15533).

**Overflow recovery**, bounded per request: compact with the normal tail and retry once → compact with a minimal tail
(current request plus latest tool batch) and retry once → stop the turn with a message that says what fills the
context. Provider errors are classified with a port of Pi's overflow patterns plus HTTP 413 and
`context_length_exceeded`.

**Guards.** One compaction in flight per Session. Three consecutive failures, or three ineffective compactions (result
at or above 0.75·T), pause auto-compaction for the Session and tell the user. Usage reported before the latest
checkpoint is ignored. Manual `/compact [focus]` works on every surface.

**Deferred**, with reasons in evaluation §4: cache-safe fork summarization (Anthropic-specific economics; decide from
the compaction cost recorded on every checkpoint row), background precomputed compaction, automatic re-read of
recently modified files, and server-side compaction (forbidden by hard constraint 8).

### 5.3 Tools (C-7)

The tool surface follows Pi: names, parameters, logic, and model-facing text. tau is the Python reference.

| Tool | Parameters | Behavior |
| --- | --- | --- |
| `read` | `path`, `offset?`, `limit?` | raw text without line numbers; head 2,000 lines / 50 KB; `Use offset=K to continue`; images attached for vision models |
| `write` | `path`, `content` | creates parent directories; overwrites |
| `edit` | `path`, `edits[{oldText, newText, replaceAll?}]` | exact match, then Pi's deterministic normalized match; unique unless `replaceAll`; non-overlapping; matched against the original; CRLF and BOM preserved |
| `bash` | `command`, `timeout?`, `watch?` | stdout and stderr merged; tail 2,000 lines / 50 KB with the full output spilled to a file; a non-zero exit is an error result; `timeout` kills the process tree |

One tool set serves every model. stdin is closed, so interactive programs fail fast instead of hanging; where tmux
is installed the agent can still drive one through `bash`, as Pi's own repository does. Search, delegation,
credentials, waiting, files for the user, and questions stay on existing Avibe surfaces (`rg`/`find` through `bash`,
`vibe agent run`, Vault, `vibe watch`, `file://` links, the question UI).

Owner additions over Pi: `edit` `replaceAll` (from Claude Code and OpenCode) and Watch-backed `bash` (below). Rejected
for v1: stale-write guard, line numbers in `read`, a `workdir` parameter, head+tail failure output, `write_stdin`,
post-edit LSP diagnostics, and dedicated `grep`/`find`/`ls`. Two places where tau falls short and Pi is the source:
the normalized edit tier, and streaming, bounded output with spill.

**Long-running commands through Watch.** Watch serves as the agent's background facility. A process cannot change
owners after it starts, so every command gets a portable handle from its first moment and never has to move.

- **Job handle.** Every `bash` call starts a job: a one-line `sh` wrapper in its own session runs the command,
  stdout and stderr go to `<state>/agent_core/jobs/<job_id>/output.log`, and the exit code is written atomically to
  `exit`. `meta.json` holds the command, cwd, absolute deadline, and process identity (`job.schema.json`). The job host
  meets recovery invariants J1–J6 (C-7 `recovery.md`). No process holds a
  pipe to the job, so any holder of the id can read it, wait on it, or kill it. Prototype results: evaluation §5.
- **Foreground** waits on the handle: the tool tails `output.log` for live progress and Pi's truncation, and
  returns when `exit` appears.
- **Handover.** `watch: true` hands the handle to Watch at once. A foreground job still running after the foreground
  window (default 120 s, configurable) is handed over instead of killed. Handover registers a once Watch on the same
  handle: nothing restarts, and the command runs exactly once.
- **Watch target kind `job`.** A Watch can target a job handle instead of a waiter command, keyed by `job_id` and
  created by adopt-or-create, so one job never has two Watches. Its cycle waits on the handle, which is idempotent
  across vibe restarts, and enforces the job's deadline; the command's own exit code is reported and never read as Watch's
  75 or 64; the Watch is displayed as the original command. The Watch owns the job: removal, disabling, lifetime
  expiry, and Session archive kill its process tree.
- **Restart and stop.** Jobs survive vibe restarts, upgrades, and crashes; an open foreground tool call is then
  settled as "still running, now Watch `<id>`". An explicit `vibe stop` ends them, matching how it already ends tool
  commands.
- **What the agent sees.** One concept, Watch; the job id is an internal detail. The tool result names the Watch, the
  output so far, the full-output path, and `vibe watch show|remove <id>`. The follow-up uses Watch's existing P1
  delivery: after the current tool batch of an active turn, or a new turn when idle. The environment block rendered
  into each consumed input (C-7) lists the Session's live Watches, so the agent keeps track of them across
  compaction.
- **Platforms.** Windows needs the equivalent wrapper (a new process group plus a job object, as
  `core/watch_worker.py` already does).
- **Engine boundary.** `core/agent_core/tools` sees a `JobHost` interface; the adapter implements handover with Watch;
  engine tests use an in-process fake.

### 5.4 Interactive terminals (planned; shapes v1, ships later)

Goal: the agent drives interactive programs the way a person does (installers with prompts, REPLs, `ssh`,
`gh auth login`, TUIs), and a person can open the same terminal and take over. The primitive is proven: Terminal-Bench's
reference agent, Terminus, has no tool other than a tmux pane; each step sends keystrokes with a wait duration and
reads back the captured screen.

Locked in v1 so the later backend needs no migration or contract change:

1. **Handle operations.** `JobHost` names `start`, `status`, `wait`, `output(since)`, and `kill`, and reserves
   `send(keys)`, `screen()`, `resize()`, and `attach_info()`. The v1 `pipe` backend implements the first five.
2. **Persisted job metadata** carries `backend` (`pipe | pty`) and a nullable `terminal{socket, target}` from the first
   release.
3. **The environment travels with the job**, not through process inheritance, so a pane created by a long-lived tmux
   server still runs with the caller's environment. A file used for this is mode 0600 and removed once read.
4. **One output normalizer** sits between every backend and the model: strip ANSI sequences, collapse carriage-return
   redraws, apply Pi's caps. It does nothing to `pipe` output and is required for `pty`.
5. **Watch targets the handle, not the backend**, so an interactive job can be watched; "wait until the screen shows
   X" is a later Watch condition on the same target.
6. **Context management** treats screen snapshots as clearable tool results.

The `pty` backend, when built: the same wrapper inside a pane of Avibe's vendored tmux on a dedicated socket
(macOS/Linux), raw output logged with `pipe-pane`, screens from `capture-pane -p`, keystrokes sent literally and chunked
under tmux's roughly 16 KB command limit, and waits that end on exit, quiet output, or a pattern rather than only a fixed
duration. The Terminal app attaches to the same pane for takeover. Windows needs a ConPTY backend. tmux is not the v1
base for the reasons measured in evaluation §5.

Open until then, decided by evaluation on Terminal-Bench through the Harbor harness (`pipe` only versus `pty`
capable): whether the agent-facing form is `bash` gaining `tty` and `session` parameters, or one dedicated terminal
tool shaped like Terminus's.

## 6. Acceptance criteria

Properties; the test suites enumerate cases.

- **A1 Control matrix.** Every control point in evaluation §1 passes natively against the hermetic stub in
  `tests/agent_core/`, with the stub's request log as evidence of what the model saw.
- **A2 Native protocol fidelity.** For every protocol in C-6's vocabulary, a session can start, call a tool, and
  continue after switching to every other protocol. Tool ids survive, provider-owned thinking is replayed only into
  its own origin, no signature is ever synthesized, and the stub sees exactly one protocol per request.
- **A3 Durability.** SIGKILL at any point resumes with no committed entry lost and no command executed twice; the
  context rebuilt from the tables is always a valid request for every protocol.
- **A4 Bounded context.** A session with more than 10,000 messages keeps every request under its model's T.
  Compaction and clearing only insert rows. A fork anchored before a checkpoint projects the full original context.
  With a provider that always overflows, one request triggers at most two compactions and then a user-visible stop.
- **A5 Egress.** With Model Hub configured, every model call goes to the resolved `base_url`: the engine itself sends
  no telemetry and never contacts a vendor directly. Network use by tools (`bash`, later MCP) is the tools' own and is
  governed by tool and workspace policy, not by this criterion.
- **A6 Surfaces.** The same turn produces equivalent user-visible outcomes on Workbench and on every IM platform in
  the Incus regression environment: progress, tool activity, final message, stop, questions, Watch follow-ups.
- **A7 Registration.** Every backend list in the tree is either the catalog's set or the native-CLI subset defined in
  C-8, and a contract test enforces it.
- **A8 Isolation.** Engine tests never touch `$HOME`, `~/.avibe`, or a live service.
- **A9 Portability.** Any session, rebuilt from the tables, continues on every other protocol with the original
  provider unreachable: all user, tool, and assistant text is present in the new request, no signature from another
  origin is sent, and the run completes.
- **A10 One copy.** For every Avibe Agent Session, the context rebuilt from `messages` and `agent_events` equals what
  the stub received, and no other file or table holds a copy of it.
- **A11 Exactly-once commands.** A command started by `bash` runs once across foreground completion, `watch: true`,
  foreground-to-Watch handover, and a vibe restart in any of those states; Watch removal ends its process tree.
- **A12 Compaction quality.** On a scripted long-session fixture compacted twice, the agent still states the user's
  constraints, the files changed and why, the last error, and the next step. This is an LLM-in-the-loop evaluation
  tracked per prompt version, not a unit test.

## 7. Delivery plan

| Phase | Lanes | Scope | Gate |
| --- | --- | --- | --- |
| P0 | docs | this plan, the evaluation, and the contract drafts | owner approval; `pr-delivery-loop` gates |
| P1 | Model Hub extension: `core/handlers/model_hub/`, Model Hub contracts, UI types · `ai`: `core/agent_core/ai/` · `agent`: `core/agent_core/agent/` · `tools`: `core/agent_core/tools/` | C-1, C-2, C-3, C-6, C-7 frozen on `master` first | each lane in its own worktree and PR; the control points it owns pass |
| P2 | adapter: `modules/agents/avibe/`, catalog, config, and UI registration (i18n strings; any new UI element needs an approved `design.pen` frame first) · transcript and Watch `job` target: `core/agent_core/harness/`, `storage/`, `core/watches.py` | C-4, C-5, C-8 frozen | A1, A3, A7, A10, A11; Incus smoke on one platform |
| P3 | context management as the main v1 investment; skills rehydration; MCP client; permissions through the question UI | C-9 frozen | A4; A12 baseline recorded |
| P4 | regression and acceptance | Incus four-platform regression; owner checklist | A6; default-agent decision |

Before P1, a few live calls through one Model Hub Source the owner names confirm the stub's fidelity. Circuit
breaker, review gates, and close-out follow the `pr-delivery-loop` skill.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Provider edge cases become permanent maintenance | port tau's fixtures; add cases derived from `pi-ai` for stop reasons, overflow, retry, SSE; one conformance suite per protocol |
| The Model Hub extension lands late and blocks C-2 conformance | the Model Hub lane runs first in P1; the `ai` lane develops against the contract file, not the live gateway |
| A second persistence model creeps back | C-5 forbids one; A10 tests it |
| Compaction quietly loses user constraints | the fixed "User instructions and constraints" heading; A12 tracked per prompt version |
| Database growth from inline tool output | output governance bounds each row; the reserved reference form allows moving it out without a schema change |
| Scope creep toward the tension system or interactive terminals | §2 non-goals; §5.4 ships only the shape in v1 |
| Python 3.10 floor versus ported code | port with `TypeAlias`; CI already covers 3.10 |
