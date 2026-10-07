# Avibe Agent core

Status: **Approved direction, contracts in draft** · 2026-10-02

- Evidence: [`avibe-agent-core-evaluation.md`](avibe-agent-core-evaluation.md)
- Contracts: [`agent-core-contracts/`](agent-core-contracts/README.md), frozen per phase before parallel lanes
  fork (§7)

## 0. Decision summary

Avibe builds its own first-party Agent in Python, inside this repository, as a fourth backend next to Claude Code,
Codex, and OpenCode. Its backend id is `vibey`, which is also its built-in Agent's name, and users meet it as "Vibey"
(renamed from `avibe` before release). "Avibe Agent" remains the engine's name in these plans. It reaches models through
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
- Becoming the default agent for new installs. v1 is the built-in backend, always enabled and listed first
  ([`avibe-agent-always-on.md`](avibe-agent-always-on.md)), but not the default for new chats; that default is decided
  after the P4 regression with real usage evidence.
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
modules/agents/vibey/             backend adapter: BaseAgent implementation, event → MessageOutput,
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
`ModelEndpoint` value; `modules/agents/vibey/` is the only package that touches controller state.

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
| C-8 | Backend registration: the catalog as the one declaration, and which sets must include `vibey` | catalog → every backend list | `backend-registration.md` |
| C-9 | Context management: projection tiers, trigger, cut point, checkpoint, overflow ladder, guards | `harness` → `agent`, adapter | `context.md`; row shapes in `transcript-rows.schema.json` |

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
| Compaction checkpoint | `agent_events` `context_compaction`, `visibility='context'` | C-9 `context.md` §7 | expandable "context compacted" marker |
| Cleared tool result | `agent_events` `context_edit`, `visibility='context'` | target row and placeholder | none |

Schema delta (one Alembic migration, nullable columns, no backfill):

1. `context_seq INTEGER NULL` on `messages` and `agent_events`, with a partial unique index
   `(session_id, context_seq) WHERE context_seq IS NOT NULL`. `agent_events.sequence` stays as it is: it is a
   released, turn-local field of the storage API, and context membership is a different meaning.
2. A new `agent_events.visibility` value, `context`, never removed by trace retention. The retention filter is
   already `event_type='tool_call' AND visibility='trace'`; a contract test pins the exemption.
3. New `agent_events.event_type` values `tool_result`, `context_compaction`, `context_edit`, and `agent_state` (hook
   state for fork and C-3), registered where the activity panel reads event types.
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

**Frozen 2026-10-04** by owner decision. The contract is [`agent-core-contracts/context.md`](agent-core-contracts/context.md);
this section summarizes it. Three tiers, cheapest first: output governance at write time (C-7); clearing old `read`
and `bash` results with `context_edit` rows, on by default; and a checkpoint written by the model, recorded as a
`context_compaction` row. Compaction and clearing only append rows.

- **Limits from Model Hub.** `W` and `L_in` come from the `context_window` and `input_limit` of the route resolved
  for the request (128,000 when unknown). `O` is the request's `max_tokens`: the hop's `max_output_tokens` (8,192
  when unknown) capped by the Agent's output budget, and `min(16,000, O)` for a checkpoint request. One pure
  function derives the rest and `est` from the final request, immediately before it is sent.
  `M = min(max(8,000, 3% W), W / 8)`, `T = min(L_in - O - M, 0.9 W)`.
- **Trigger** before every model request, including inside the tool loop, on the final request (in v1 no user hook
  runs with context management): `est` is the usage of the latest response stored with its request facts, adjusted by the UTF-8 bytes / 4
  difference between this request and that one (1,600 per image), while that request went to the same route and the
  transcript up to the response is unchanged; otherwise UTF-8 bytes / 4 of the whole request. Compact when
  `est >= T`.
- **Cut** before a user message or before an assistant message whose tool batch follows it, keeping
  `min(20K, 0.25 T)` verbatim; a cut inside the in-flight Turn keeps its inputs (the first and each steer) as they
  were, images included. A tool batch that cannot fit even with the conversation moved out is cut to fit, each
  result saying so.
- **Checkpoint delivery is a fork**: the same model, system prompt, tools, tool choice, and reasoning settings, the
  conversation prefix byte-identical, and the owner-approved three-layer prompt appended (`checkpoint-v2`). The
  checkpoint turn runs through the same loop under a "dreaming" tool policy: `read` allowed, writes only inside the
  Session's scratch directory, everything else denied; at most 5 tool rounds, each tool bounded by the room left in
  the window. Its messages are audit rows, never context.
- **After a checkpoint** the request is the rebuilt system prompt, the in-flight Turn's kept inputs as they were, the
  `<context-checkpoint>` message (framing, checkpoint, cumulative `<artifacts>`, an `<earlier-record>` hint), state
  rendered from its own stores when the checkpoint was written (the environment's core fields; the skills it loaded
  are listed by name in the checkpoint, for the model to load again), and the verbatim tail. No synthetic "continue" message.
- **Overflow ladder**, bounded per request: the normal checkpoint; fork-summarize the prefix up to the cut nearest
  half the tokens, moved earlier until it fits (rolling); with no model call, move the earliest part out (dropped); stop and say what fills the context.
- **Guards**: one compaction in flight per Session; after 2 failed or ineffective checkpoints in one Turn, the Turn
  stops compacting, silently, and a request that then cannot fit ends it; nothing of the bound is persisted, so the
  next Turn tries again. Compaction is invisible: there is no manual `/compact`
  (owner decision, 2026-10-05), and the only user-visible text is the stop message of a context that cannot fit,
  which suggests starting a new session with `/new`.
- **Deferred**: background precompute, server-side compaction (hard constraint 8), automatic re-read of modified
  files, memory tools, and a lower effort for the checkpoint turn.

Delivery: the core (`core/agent_core`, this contract) landed in PR #2360; the Avibe Agent integration (Model Hub
capabilities, the scratch directory, the `<earlier-record>` hint, state rendering, the stop message, and an end-to-end
hermetic test) is the second PR (`context.md` §9).

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
- **A4 Bounded context.** A session with more than 10,000 messages keeps every request within its model's input
  limit, and under its T unless the Turn has stopped compacting or a checkpoint for that request failed (C-9 §3).
  Compaction and clearing only insert rows. A fork anchored before a checkpoint projects the full original context.
  With a provider that always overflows, one request makes at most two checkpoint model calls and then ends in a
  user-visible stop (C-9 `context.md` §8).
- **A5 Egress.** With Model Hub configured, every model call goes to the resolved `base_url`: the engine itself sends
  no telemetry and never contacts a vendor directly. Network use by tools (`bash`, later MCP) is the tools' own and is
  governed by tool and workspace policy, not by this criterion.
- **A6 Surfaces.** The same turn produces equivalent user-visible outcomes on Workbench and on every IM platform in
  the Incus regression environment: progress, tool activity, final message, stop, questions, Watch follow-ups.
- **A7 Registration.** Every declaration that stands for a whole backend universe equals the catalog's set or the
  native-CLI subset, and every capability-specific set is classified with its relation to one of them (C-8); a contract
  test enforces both.
- **A8 Isolation.** Engine tests never touch `$HOME`, `~/.avibe`, or a live service.
- **A9 Portability.** Any session, rebuilt from the tables, continues on every other protocol with the original
  provider unreachable: all user, tool, and assistant text is present in the new request, no signature from another
  origin is sent, and the run completes.
- **A10 One copy.** For every request without a transient `before_model` rewrite (C-3 §3), the context rebuilt from
  `messages` and `agent_events` equals what the stub received; no other file or table holds a copy of it. A
  checkpoint turn's request is that context (for a rolling fork, its prefix up to the cut) plus the checkpoint
  request and the turn's admitted responses and tool results (C-9 §6); its audit row also keeps the attempts that
  were retried or failed.
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
| P2 | adapter: `modules/agents/vibey/`, catalog, config, and UI registration (i18n strings; any new UI element needs an approved `design.pen` frame first) · transcript and Watch `job` target: `core/agent_core/harness/`, `storage/`, `core/watches.py` | C-4, C-5, C-8 frozen | A1, A3, A7, A10, A11; Incus smoke on one platform |
| P3 | context management as the main v1 investment; loaded skills listed by name across checkpoints; MCP client; permissions through the question UI (pending questions live in the controller process while Workbench answers arrive through `vibe/ui_server.py`, so this needs the controller IPC path, not the in-memory `QuestionUIHandler` alone) | C-9 frozen | A4; A12 baseline recorded |
| P4 | regression and acceptance | Incus four-platform regression; owner checklist | A6; default-agent decision |

Before P1, a few live calls through one Model Hub Source the owner names confirm the stub's fidelity. Circuit
breaker, review gates, and close-out follow the `pr-delivery-loop` skill.

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| Provider edge cases become permanent maintenance | port tau's fixtures; add cases derived from `pi-ai` for stop reasons, overflow, retry, SSE; one conformance suite per protocol |
| The Model Hub extension lands late and blocks C-2 conformance | the Model Hub lane runs first in P1; the `ai` lane develops against the contract file, not the live gateway |
| A second persistence model creeps back | C-5 forbids one; A10 tests it |
| Compaction quietly loses user constraints | the fixed "User requirements" heading of `checkpoint-v2`; A12 tracked per prompt version |
| Database growth from inline tool output | output governance bounds each row; the reserved reference form allows moving it out without a schema change |
| Scope creep toward the tension system or interactive terminals | §2 non-goals; §5.4 ships only the shape in v1 |
| Python 3.10 floor versus ported code | port with `TypeAlias`; CI already covers 3.10 |

## 9. Open review notes

Codex findings on this PR are advisory by owner decision (2026-10-02) because it is a spec-only PR; the lanes'
code reviews carry the precision. These notes from the last review round are kept for the next revision of the
contracts and checked against the implementing lane's code before they are closed.

| Priority | File | Note |
| --- | --- | --- |
| P1 | `model-hub-consumer.md` | Apply the full target transform during failover |
| P2 | `transcript.md` | Give display-only outbox rows an ordering key |
| P2 | `message.schema.json` | Preserve malformed-argument state on tool calls |
| P2 | `tools.md` | Reject nonpositive bash timeouts |
| P1 | `job.schema.json` | Require process state in every persisted job record |
| P2 | `loop-control.md` | Define the terminating tool-result field |
| P2 | `avibe-agent-core.md` | Scope portability to the current projected context |
| P1 | `avibe-agent-core.md` | Keep background descendants inside the job lifecycle |
| P2 | `loop-control.md` | Avoid delivering refusal errors twice |
| P2 | `message.schema.json` | Require content in canonical tool results |

## 10. Follow-ups

| Item | Owner | Note |
| --- | --- | --- |
| Product-wide media retention | `storage/media_service.py` | No media file is removed from disk today, whether Workbench upload, IM attachment, or Avibe Agent context snapshot (`<state>/agent_core/media`); session deletion only clears or cascades `media_objects` references. Retention needs one owner for every source. It must be fork-aware: a fork descendant keeps replaying the image tokens of a source Session that was deleted. Recorded as a v1 known limit in PR #2345. |
| Steer receipt fencing in the shared Turn owner | `core/session_turns.py` (`_finish_steer`) | **Done in PR #2345** (orchestrator-authorized cross-lane fix): a negative receipt settles only a current attempt whose Deliveries are still steering or reconciling, and a caller that knows the attempt passes `expected_attempt_id`, so a late or duplicate receipt can no longer pull a Delivery out of a Turn that claimed it. Regression: `test_a_late_negative_steer_receipt_never_moves_a_delivery_its_attempt_no_longer_owns`. |
| Live partial text and progress | every backend, Workbench and IM | No backend shows streamed partial text or live tool output today; the Avibe Agent drops `text_delta`, `thinking_delta`, and `tool_progress` (`loop-control.md` §6). Showing them needs a new UI surface for every backend, not an Avibe Agent change. |
| Slash commands are actions | `core/session_turns.py`, `storage/message_deliveries.py`, `storage/messages_service.py` (every backend) | A user message that is a slash command (`/model`, `/memory`, … for the native backends) is an ordinary user row today, so each consumer re-parses text: Delivery segmentation can merge it with neighbouring queued messages, and title backfill can take it as the first prompt. Classify once at ingestion (a `metadata.command` marker, one helper as the legacy fallback); a command delivery never merges; titles skip command rows by the marker. Not part of the C-9 wave: the Avibe Agent has no slash command of its own (owner decision, 2026-10-05). |
| Hooks with context management | `core/agent_core/agent/` (C-3, C-9) | In v1, an Agent with a `ContextConfig` takes no user hooks: `before_model` rewrites and `before_tool`/`after_tool` decisions each changed what C-9 owns (the route, the projected prefix, artifacts, the checkpoint budget). A post-v1 design defines which hook outputs C-9 accepts and how it accounts for them before the two are combined (owner decision, PR #2360 round 5). |
| Call-instance identity for job files | `core/agent_core/tools/` (`ToolContext`, job `meta.json`), the adapter | A tool call's identity is its instance: the owning response plus the call id, because providers reuse ids. Rows are matched by context order; job files still fall back to the clock (`find_job(created_since)`, J5's result-after-creation check), which assumes a non-decreasing wall clock between a response's commit and its job's start. Threading the call instance (owning response row and `context_seq`) into `ToolContext` and the job meta would remove that last residual. |
