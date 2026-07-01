# Agent Run Harness

## Background

Vibe Remote needs three user-facing automation surfaces:

- manual/external Agent execution;
- scheduled execution;
- watched/condition-triggered execution.

These are different product entry points, but they should share the same
execution schema, status model, history, and management logic.

The old `vibe hook send` command is better understood as "enqueue one Agent
Run" rather than as a separate product concept. This plan replaces hook with an
Agent Run harness.

## Product Model

```text
Agent Run = execute one Agent job.
Task = a time trigger that creates Agent Runs.
Watch = a condition trigger that creates Agent Runs.
```

This gives the Web UI a clean shape:

- Agent Runs tab: runs created by CLI, webhook, manual action, or API.
- Scheduled Tasks tab: time-based trigger definitions.
- Watches tab: waiter/monitor trigger definitions.

The shared primitive is the run record, not the task.

## Goals

- Replace `vibe hook send` with `vibe agent run`.
- Use `--message` / `--message-file`, not `--prompt`, for Agent Run user message.
- Support synchronous and asynchronous Agent Runs.
- Support running inside an existing `session_id`.
- Support creating a new session and returning the reserved Session ID.
- Let Agent Run, Task, and Watch all accept `--agent <name>` as the command-level
  Vibe Agent selector.
- Let tasks and watches reuse the same run spec and run history.
- Prepare for webhook-triggered Agent Runs.

## CLI Design

### `vibe agent run`

Asynchronous Agent Run (default):

```bash
vibe agent run --agent release-reviewer --no-callback --message "Review this diff."
```

Synchronous Agent Run:

```bash
vibe agent run --agent release-reviewer --sync --message-file request.md
```

Continue an existing session:

```bash
vibe agent run --session-id sesk8m4q2p7x --message "Continue the investigation."
```

Create a new session in a scope:

```bash
vibe agent run \
  --create-session \
  --scope-id slack::channel::C123 \
  --message "Start a fresh incident triage."
```

Rules:

- Use `--message` and `--message-file` for the user/task message.
- `--prompt` / `--prompt-file` are deprecated compatibility inputs. If a user
  passes them, reject the command with a clear message that points to
  `--message` / `--message-file`.
- Exactly one message source is required.
- `--session-id` continues a conversation by public Vibe Session ID.
- `--create-session` reserves a new Vibe Session ID. With `--scope-id`, the
  session is placed in that Scope; without scope placement, a direct Agent Run
  creates a private/no-delivery session for agent harness or sub-agent usage and
  requires explicit `--agent`, then returns `session_id` for later continuation.
- `--agent <name>` selects the Vibe Agent for this run. If omitted, resolve the
  Agent from the session or placement Scope defaults.
- `--same-scope` places a new Session in the caller/source Scope.
- `--scope-id <scope-id>` places a new Session in a specific existing Scope.
- Runs return after they are queued by default.
- `--sync` waits for completion and prints the result.
- `--async` remains a compatibility alias for the default queued behavior.

Execution controls:

- The run uses the scope/session workdir when a scope/session is present;
  otherwise it uses the service default workdir.
- `--wait-timeout <seconds>` controls how long a `--sync` command waits; it
  does not terminate the run. There is no fixed default wait limit; if a
  synchronous run exceeds 30 minutes, the CLI returns an accepted response and
  the run continues asynchronously. The 30-minute threshold is a system
  protection threshold, not a user-visible default timeout.
- `--json` is the stable machine-readable contract. Non-JSON output is for
  humans and may be more compact.

### Deprecating `vibe hook send`

Compatibility window:

```bash
vibe hook send ... -> vibe agent run ...
```

Rules:

- Keep accepting the old command temporarily.
- Return a deprecation warning.
- Do not teach `vibe hook` in new prompt guidance or docs.

### `vibe task`

`vibe task` manages saved time triggers. It does not execute work directly; it
creates or manages `run_definitions` rows with `definition_type=scheduled`.

Create a recurring task:

```bash
vibe task add \
  --cron "0 9 * * *" \
  --agent release-reviewer \
  --message "Prepare the daily release review."
```

Create a one-shot managed task:

```bash
vibe task add \
  --at "2026-06-01T09:00:00+08:00" \
  --create-session \
  --scope-id slack::channel::C123 \
  --message-file request.md
```

Manage a task:

```bash
vibe task list
vibe task show <task-id>
vibe task update <task-id> --cron "*/30 * * * *"
vibe task run <task-id>
vibe task pause <task-id>
vibe task resume <task-id>
vibe task remove <task-id>
```

Rules:

- Exactly one schedule source is required: `--cron` or `--at`.
- Exactly one message source is required: `--message` or `--message-file`.
- `--agent <name>` selects the Vibe Agent stored in this task definition.
- If `--agent` is omitted, resolve the Agent from the target Scope/session when
  the task is created.
- `task update <id> --agent <name>` changes future runs of that task; historical
  `agent_runs` keep their captured run spec.
- Existing `--prompt` / `--prompt-file` should be recognized only to return a
  deprecation error that points to `--message` / `--message-file`.
- `--create-session-per-run` is valid for recurring tasks and rejected for
  `--at` one-shot tasks.
- `task run <id>` creates an immediate `agent_runs` row from the saved
  definition; it does not mutate the schedule.
- `pause` disables future scheduled firing without deleting history.
- `remove` soft-deletes the definition.

### `vibe watch`

`vibe watch` manages saved condition triggers. A watch runs a waiter command,
observes its terminal state, and creates an Agent Run when there is something to
report.

Create a one-shot watch:

```bash
vibe watch add \
  --agent release-reviewer \
  --message "The export finished. Summarize the result." \
  -- python3 scripts/wait_for_export.py
```

Create a long-running watch:

```bash
vibe watch add \
  --forever \
  --retry-exit-code 75 \
  --retry-delay 60 \
  --create-session-per-run \
  --scope-id slack::channel::C123 \
  --message "A CI event finished. Review the waiter output." \
  -- python3 scripts/wait_for_ci.py
```

Manage a watch:

```bash
vibe watch list
vibe watch show <watch-id>
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

Rules:

- Watch definitions live in `run_definitions` with `definition_type=watch`.
- The waiter command is part of the trigger configuration.
- `--agent <name>` selects the Vibe Agent stored in this watch definition.
- If `--agent` is omitted, resolve the Agent from the target Scope/session when
  the watch is created.
- `--message` / `--message-file` is the instruction template for the Agent Run that
  is created after the waiter reaches a reportable state.
- Existing `--prefix` can remain as a compatibility alias for prepending an
  instruction before waiter stdout, but the target schema should store this as
  `message.text` plus structured waiter output.
- `--forever` watches continue running until paused, removed, lifetime timeout,
  or a non-retryable terminal failure.
- Watch runtime process state may continue to use `run_type=watch_runtime`, but
  user-visible follow-up executions should use `run_type=watch`.

### One-Off Agent Run vs One-Off Task

`vibe agent run` and `vibe task add --at ...` can both execute once, but
they answer different product needs:

- Agent Run: execute now; no saved definition; managed through run history.
- One-shot Task: execute later; saved definition; can be listed, shown,
  updated, paused, resumed, removed, or manually run before its scheduled time.

## RunSpec

All immediate runs, scheduled tasks, watches, and future webhook triggers should
share a common run spec.

```text
RunSpec
  agent_target:
    mode: named_agent | session
    agent_name
    session_id
  session_target:
    mode: none | existing | create_once | create_per_run
    session_id
    scope_id
  delivery_target:
    mode: none | scope
    scope_id
  message:
    text
    payload_json
  execution:
    mode: sync | async
```

Notes:

- `message.text` is user/task message, not system prompt.
- Agent system prompt comes from the Vibe Agent catalog.
- `payload_json` is reserved for webhook/API structured input.
- `create_per_run` belongs to trigger definitions, not one-off direct runs.

## Session And Delivery Targeting

Background commands must keep three identities separate:

- Agent Session: the Vibe session to continue or create.
- Delivery Scope: the IM scope where output should be delivered.
- Agent Definition: the Vibe Agent that supplies backend/model/effort/prompt.

### Scope ID

`--scope-id` uses `scopes.id`. It is not a session key.

Example shape:

```text
<platform>::<scope_type>::<native_id>
```

Examples:

```text
slack::channel::C123
slack::user::U123
lark::channel::oc_...
```

No separate thread anchor parameter is needed. If thread becomes a first-class
Scope later, the same `--scope-id <scope-id>` mechanism covers it.

### Session Policies

Existing session:

```bash
--session-id <agent-session-id>
```

Create one reusable session:

```bash
--create-session --scope-id <scope-id>
```

Create a fresh session for every trigger execution:

```bash
--create-session-per-run --scope-id <scope-id>
```

Rules:

- `--session-id`, `--create-session`, and `--create-session-per-run` are
  mutually exclusive.
- `vibe agent run --create-session` may omit scope placement to create a
  private/no-delivery Session, but it must explicitly pass `--agent`. Managed
  task/watch definitions that use `--create-session` or `--create-session-per-run`
  require `--same-scope` or `--scope-id`.
- `--create-session` reserves a Vibe Session ID immediately. Runtime binds
  backend-native state on the first execution.
- `--create-session-per-run` stores a policy on `run_definitions`; every
  execution creates a new Vibe Session ID and records it on the corresponding
  `agent_runs` row.
- Reject `--create-session-per-run` for one-shot `task add --at`, because it is
  equivalent to `--create-session` but less clear.
- Immediate `vibe agent run` only needs `--create-session`; per-run has no
  separate meaning for a single direct run.

### Parameter Mutual-Exclusion Matrix

| Parameter combination | `vibe agent run` | `vibe task add/update` | `vibe watch add` | Rule |
| --- | --- | --- | --- | --- |
| `--message` + `--message-file` | Reject | Reject | Reject | Each definition or execution has exactly one message source. |
| `--prompt` / `--prompt-file` + any message parameter | Reject | Reject | Reject | Legacy parameters only produce deprecated guidance. |
| `--session-id` + `--create-session` | Reject | Reject | Reject | A run has one Session policy. |
| `--session-id` + `--create-session-per-run` | Reject | Reject | Reject | `existing` conflicts with `create_per_run`. |
| `--create-session` + `--create-session-per-run` | Reject | Reject | Reject | `create_once` conflicts with `create_per_run`. |
| `--create-session` without scope placement | Allow only with `--agent` | Reject | Reject | Direct Agent Runs may create private/no-delivery Sessions, but the Agent must be explicit; managed definitions need a Scope when creating Sessions. |
| `--create-session-per-run` without scope placement | N/A | Reject | Reject | Every per-run Session needs a Scope. |
| `--create-session-per-run` + `task add --at` | N/A | Reject | N/A | One-shot tasks run once; use `--create-session`. |
| `--agent` + `--session-id` | Allow if backend matches | Allow if backend matches | Allow if backend matches | `--agent` only overrides this run/definition and does not mutate the Session; reject if Agent backend differs from Session backend. |
| default async + `--wait-timeout` | Reject | N/A | N/A | `--wait-timeout` only controls `--sync` CLI waiting and does not control async run lifetime. |
| `--agent` + `--scope-id` | Allow | Allow | Allow | `--agent` overrides the Scope default Agent; `--scope-id` controls placement. |

### Delivery Policies

For newly created sessions that should live in an existing Scope, use
`--same-scope` or `--scope-id <scope-id>` because the CLI cannot infer a safe IM
or Workbench placement outside an injected caller context. Direct
`vibe agent run --create-session` without scope placement creates a
private/no-delivery Session and returns run/session output only.

For existing sessions, ordinary delivery comes from the Session's stored Scope.
New help and docs should not teach one-off transport override flags.

### Runtime Target Resolution

Background commands should not accept backend/model/effort override flags.
Runtime target resolution is:

1. If `--agent <name>` is provided, load that Vibe Agent.
2. Otherwise, if `--session-id` is provided, use the session's current Agent
   identity.
3. Otherwise, resolve `--same-scope` or `--scope-id` to a Scope and load the
   Scope's selected Vibe Agent.
4. If no Scope Agent exists, use the configured system default Agent.
5. If both `--agent` and `--session-id` are provided, verify that the Agent
   backend matches the Session backend; reject on mismatch because cross-backend
   continuation cannot preserve context.
6. Use the resolved Agent backend/model/effort/system prompt.
7. Use the Scope/session workdir when available; otherwise use the service
   default workdir.

This makes `--agent` a command-level override of the Scope default Agent, not a
backend/model/effort override. When combined with `--session-id`, it only affects
the current run and does not mutate the Session's future default Agent.

## Storage Model

Rename and evolve the existing two background tables:

- `background_tasks` -> `run_definitions`: definition table for reusable
  managed triggers.
- `background_runs` -> `agent_runs`: execution table for every actual run.

### `run_definitions`: Definitions

`run_definitions` stores definitions that may create runs later. It is the
unified definition table for tasks, watches, and future webhooks.

Recommended `definition_type` values:

- `scheduled`: cron or one-shot scheduled task;
- `watch`: managed waiter that produces a follow-up run when it reaches a
  terminal condition;
- `webhook`: future external trigger definition.

Immediate `vibe agent run` does not create a `run_definitions` row. It creates
a `agent_runs` row directly.

Core definition semantics should be stored as first-class columns. Field
details:

| Field | Status | Definition | Design intent |
| --- | --- | --- | --- |
| `id` | Existing | Definition ID. | Stable management handle for task/watch/webhook definitions. |
| `definition_type` | Existing field renamed, extended values | Definition type: `scheduled`, `watch`, or future `webhook`. | Rename from `task_type` so watch/webhook definitions are not called tasks. |
| `name` | Existing | User-visible definition name. | Show readable names in CLI and Web UI. |
| `agent_name` | New | Vibe Agent selected for future runs. | Make task/watch Agent selection explicit; if omitted, store the resolved Scope/session default. |
| `session_policy` | New | `existing`, `create_once`, or `create_per_run`. | Persist the lifecycle semantics for Session reuse/creation. |
| `session_id` | Existing | Existing or reserved Vibe Session ID when policy uses one. | Let future executions continue the same Vibe Session. |
| `legacy_session_key` | Existing, compatibility | Legacy target from old records and commands. | Migration/display only; new writes prefer `session_id` / `scope_id`. |
| `scope_id` | Target field | Session placement Scope ID. | Decouple placement from Session identity and make Scope selection explicit. |
| legacy delivery fields | Existing, compatibility | Old delivery override columns. | Preserve old records and hidden compatibility inputs only; do not expose them in help, docs, prompts, or new examples. |
| `prompt` | Existing, compatibility | Legacy message template field. | Read/write compatibility during migration; target schema uses `message`. |
| `message` | New | Stored Agent message template. | Align storage with `--message` and separate user message from Agent system prompt. |
| `message_payload_json` | New | Optional structured message payload. | Support webhook/API structured input without overloading text. |
| `schedule_type` | Existing | `cron` or `at`. | Distinguish recurring and one-shot schedules. |
| `cron` | Existing | Cron expression. | Store recurring schedule. |
| `run_at` | Existing | One-shot scheduled time. | Store one-time schedule. |
| `timezone` | Existing | Schedule timezone. | Make schedule interpretation reproducible. |
| `command_json` | Existing | Watch waiter argv. | Store structured command without shell quoting ambiguity. |
| `shell_command` | Existing | Watch waiter shell command. | Support shell mode and legacy command shapes. |
| `prefix` | Existing, compatibility | Legacy watch follow-up instruction prefix. | Keep `--prefix` compatibility; target semantics fold this into `message`. |
| `cwd` | Existing | Watch/runtime working directory. | Background execution should not depend on service process cwd. |
| `mode` | Existing | Watch mode such as `once` or `forever`. | Persist watch lifecycle. |
| `timeout_seconds` | Existing | Per-run or per-cycle timeout. | Bound individual execution time. |
| `lifetime_timeout_seconds` | Existing | Overall watch lifetime timeout. | Bound long-running watch lifetime. |
| `retry_exit_codes_json` | Existing | Retryable waiter exit codes. | Distinguish keep-waiting from terminal failure. |
| `retry_delay_seconds` | Existing | Retry delay for retryable waiters. | Control retry cadence for forever watches. |
| `enabled` | Existing | Whether the definition may create future runs. | Support pause/resume without deleting history. |
| `deleted_at` | Existing | Soft-delete timestamp. | Hide removed definitions while keeping historical runs. |
| `created_at` | Existing | Creation time. | Audit and ordering. |
| `updated_at` | Existing | Latest update time. | Management UI, sync, and debugging. |
| `last_started_at` | Existing | Latest execution start time. | Fast list summary. |
| `last_finished_at` | Existing | Latest execution finish time. | Fast completion summary. |
| `last_event_at` | Existing | Latest watch event time. | Watch list summary. |
| `last_run_at` | Existing | Latest scheduled trigger time. | Task list summary. |
| `last_error` | Existing | Latest error summary. | Surface actionable failures in lists. |
| `last_exit_code` | Existing | Latest waiter/process exit code. | Watch/debug summary. |
| `last_run_id` | New | Latest user-visible run created by this definition. | Fast navigation between definition and run history. |
| `metadata_json` | Existing, extension | Non-core extension data. | Store only non-queryable backend-specific metadata, UI hints, or experimental fields. |

`metadata_json` remains available for non-core extension data only:

- backend-specific import metadata;
- UI display hints that do not affect execution;
- webhook auth/source metadata that has no query requirement yet;
- experimental fields before they graduate to columns.

Implementation can migrate incrementally by keeping the existing `prompt` column
as a compatibility alias for `message` until a schema migration renames or
duplicates it.

Definition table migration priority:

1. Add `agent_name`, `session_policy`, `message`, `message_payload_json`, and
   `last_run_id`.
2. For new definitions, write the new columns and any required compatibility
   columns such as `prompt`.
3. When reading old definitions, derive `message` from `prompt` if `message` is
   empty.
4. After the compatibility window, decide whether to remove or hide legacy
   semantic fields such as `prompt` and `prefix`.

### `agent_runs`: Executions

`agent_runs` stores every actual execution:

- immediate `vibe agent run`;
- async `vibe agent run`;
- scheduled task fire;
- watch terminal event/follow-up;
- future webhook invocation.

Recommended `run_type` values:

- `agent_run`: direct agent invocation, usually with `definition_id = null`;
- `scheduled`: execution created from a scheduled `run_definitions` row;
- `watch`: execution created from a watch `run_definitions` row;
- `webhook`: execution created from a webhook `run_definitions` row;
- `watch_runtime`: legacy/runtime waiter bookkeeping if still needed.

Target domain statuses:

- `queued`;
- `running`;
- `succeeded`;
- `failed`;
- `canceled`.

The storage layer can initially map these to existing values such as `pending`,
`processing`, and `completed`; the public output schema should use the domain
statuses above.

Field details:

| Field | Status | Definition | Design intent |
| --- | --- | --- | --- |
| `id` | Existing | Run ID. | Stable handle for show/list/cancel/history. |
| `definition_id` | Existing field renamed | Optional source `run_definitions.id`. | Rename from `task_id` because the source is a generic definition, not only a task. |
| `run_type` | Existing, extended values | `agent_run`, `scheduled`, `watch`, `webhook`, or runtime bookkeeping type. | Distinguish direct runs, task runs, watch follow-ups, and webhook invocations. |
| `status` | Existing, normalized values | `queued`, `running`, `succeeded`, `failed`, `canceled`. | Shared status machine for CLI, Web UI, and agent harness callers. |
| `source_kind` | New | `cli`, `api`, `scheduler`, `watch`, or `webhook`. | Record who created the run for audit and filtering. |
| `source_actor` | New | Optional actor/user/system identifier. | Distinguish human, agent, scheduler, and external-system triggers. |
| `parent_run_id` | New | Parent run ID. | Support harness/sub-agent call chains and recursion guards. |
| `agent_name` | New | Vibe Agent captured for this run. | Make history auditable even if Agent definitions change later. |
| `agent_id` | New | Optional Agent ID snapshot. | Preserve relation if display names ever change; nullable if name remains immutable. |
| `agent_backend` | New | Backend snapshot. | Debug/history without joining the current Agent definition. |
| `model` | New | Model snapshot. | Explain historical behavior after Agent model changes. |
| `reasoning_effort` | New | Effort snapshot. | Explain historical behavior after Agent effort changes. |
| `session_policy` | New | Session resolution policy used for this run. | Explain how `session_id` was obtained. |
| `session_id` | Existing | Actual Vibe Session ID used by this run. | Continue conversation and query history by session. |
| `legacy_session_key` | Existing, compatibility | Legacy target for old imported runs. | Migration/display only. |
| `scope_id` | Target field | Scope placement snapshot. | Audit placement and query runs by Scope. |
| legacy delivery fields | Existing, compatibility | Old delivery override snapshots. | Preserve old run history only; new user-facing contracts should prefer Scope placement and Session callback fields. |
| `prompt` | Existing, compatibility | Legacy message field. | Read compatibility for old runs; target schema uses `message`. |
| `message` | New | Actual message sent to the Agent. | Align with `--message` and separate user message from system prompt. |
| `message_payload_json` | New | Optional structured payload. | Support webhook/API structured input. |
| `result_text` | New | Final user-visible result when available. | Support sync run output, run show, and Web UI summaries. |
| `result_payload_json` | New | Optional structured result. | Machine-readable result for API/webhook/harness use. |
| `message_ids_json` | New | IM message IDs emitted by this run. | Delivery audit, thread association, and UI links. |
| `cancel_requested` | New | Whether cancellation was requested. | Preserve real execution terminal state after best-effort cancel. |
| `cancel_requested_at` | New | Cancellation request time. | Audit cancellation requests and worker response latency. |
| `pid` | Existing | Runtime/watch process ID. | Runtime management and diagnostics. |
| `exit_code` | Existing | Process exit code. | Diagnose watch/waiter or backend process failures. |
| `error` | Existing | Error summary. | Surface failure reason in CLI/Web UI. |
| `stdout` | Existing | Bounded stdout. | Preserve waiter/backend output summary. |
| `stderr` | Existing | Bounded stderr. | Preserve diagnostic output. |
| `created_at` | Existing | Run creation time. | Queue and history ordering. |
| `started_at` | Existing | Execution start time. | Queue wait and duration calculations. |
| `completed_at` | Existing | Execution completion time. | Duration and terminal-state checks. |
| `updated_at` | Existing | Latest update time. | Worker polling and stale-running detection. |
| `metadata_json` | Existing, extension | Non-core extension data. | Store only non-queryable backend-specific or experimental data. |

For scheduled/watch/webhook runs, these columns are populated as a snapshot from
the definition at run creation time. This keeps historical runs auditable even
if the task, watch, or Agent definition changes later.

`metadata_json` remains an extension field for non-queryable, backend-specific,
or experimental data. It is not the storage location for core run semantics.

Run table migration priority:

1. Add `source_kind`, `source_actor`, `parent_run_id`, Agent snapshot fields,
   `session_policy`, `message`, `message_payload_json`, result fields, and
   emitted-message fields.
2. For new runs, snapshot first-class columns from the definition or direct
   command parameters.
3. When reading old runs, derive `message` from `prompt` if `message` is empty;
   if `result_text` is empty, continue deriving summaries from `stdout` /
   `error`.
4. Keep reading existing historical extension data from `metadata_json`, but do
   not write new core semantics there.

### Table Responsibilities

- `run_definitions` owns definition lifecycle and trigger configuration.
- `agent_runs` owns execution lifecycle, history, output, and errors.
- One-off agent runs are executions without a saved definition.
- Webhooks can be added as another definition type plus another run type.
- Harness/sub-agent usage is represented by `parent_run_id` on runs.

### Indexing Notes

The current indexes already cover the common task/watch queries. The run table
should also support:

- worker polling: `(status, updated_at)` or `(status, created_at)`;
- run history by type: `(run_type, status, created_at)`;
- session history: `(session_id, created_at)`;
- definition history: `(definition_id, created_at)`.

Migration should rename the old `agent_runs.task_id` column to `definition_id`,
or add `definition_id` first and backfill it from the old column. The database
column should consistently be `definition_id`.

## Relationship To Task And Watch

### `vibe task`

`vibe task` manages scheduled trigger definitions.

```bash
vibe task add --cron "0 9 * * *" --agent release-reviewer --message "Daily review"
vibe task run <task-id>
vibe task pause <task-id>
```

When the schedule fires, it creates a `agent_runs` row by snapshotting the
task definition columns.

### `vibe watch`

`vibe watch` manages condition trigger definitions.

```bash
vibe watch add --agent release-reviewer --message "CI finished" -- python wait.py
```

When the waiter succeeds or reaches a terminal failure, it creates a
`agent_runs` row.

### `vibe agent run`

`vibe agent run` creates a `agent_runs` row immediately. It does not create
a `run_definitions` definition unless a future command explicitly asks to save
one.

## Output Contract

JSON output should be the stable contract for agents and scripts.

General rules:

- Top-level output must include `schema_version`, `ok`, and `kind`.
- `kind` identifies the returned object type, such as `agent_run`,
  `run_definition`, or `agent_runs`.
- Human output may be shorter, but `--json` output should be stable.

Synchronous Agent Run success:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "succeeded",
    "session_id": "sesk8m4q2p7x",
    "result_text": "..."
  }
}
```

Asynchronous Agent Run accepted, or a synchronous run converted to async after
30 minutes:

```json
{
  "schema_version": 1,
  "ok": true,
  "accepted": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "queued",
    "session_id": "sesnew12345"
  }
}
```

Task/Watch definition creation success:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "run_definition",
  "definition": {
    "id": "def123",
    "definition_type": "scheduled",
    "enabled": true,
    "agent_name": "release-reviewer",
    "session_policy": "create_once",
    "session_id": "sesnew12345",
    "scope_id": "slack::channel::C123",
    "next_run_at": "2026-06-01T09:00:00+08:00"
  },
  "warnings": []
}
```

`vibe task run <id>` success:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "definition": {
    "id": "def123",
    "definition_type": "scheduled"
  },
  "run": {
    "id": "run123",
    "status": "queued",
    "definition_id": "def123",
    "agent_name": "release-reviewer",
    "session_id": "sesnew12345"
  }
}
```

`vibe runs show <id>`:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "run_type": "agent_run",
    "status": "running",
    "source_kind": "cli",
    "agent_name": "release-reviewer",
    "session_id": "sesnew12345",
    "definition_id": null,
    "created_at": "2026-05-21T17:00:00Z",
    "started_at": "2026-05-21T17:00:03Z",
    "completed_at": null,
    "result_text": null,
    "error": null
  }
}
```

`vibe runs list`:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_runs",
  "runs": [
    {
      "id": "run123",
      "run_type": "agent_run",
      "status": "running",
      "agent_name": "release-reviewer",
      "session_id": "sesnew12345",
      "definition_id": null,
      "created_at": "2026-05-21T17:00:00Z"
    }
  ]
}
```

`vibe runs cancel <id>`:

```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "agent_run",
  "run": {
    "id": "run123",
    "status": "canceled"
  }
}
```

Cancellation rules:

- Runs that have not started should be marked `canceled` directly.
- Runs already executing in a backend use best-effort cancellation in V1. If the
  runtime can interrupt them, it should; otherwise it records cancel requested
  and lets the worker/backend write the final terminal state.
- If the backend completes normally after cancel was requested, preserve the
  actual terminal `succeeded` or `failed` status and keep
  `cancel_requested=true`; do not overwrite real success/failure as `canceled`.

Failure contract:

- CLI/runtime infrastructure failure should exit non-zero with `ok: false`.
- If the target Agent ran and produced an Agent-level failure, return a run
  record with `ok: true` only when the command itself completed successfully and
  the failure is represented in `run.status` / `run.error`.
- `run.status=failed` should be enough for harness callers to branch without
  parsing text.

Run inspection and management:

```bash
vibe runs show run123
vibe runs list
vibe runs cancel run123
```

## Runtime And Recursion Policy

`vibe agent run` should always enqueue a `agent_runs` row and let the Vibe
runtime execute it.

Synchronous `vibe agent run --sync` should still create a run record first, then either:

- execute through the local runtime service and wait for completion; or
- claim/execute the run inline only if the runtime service is unavailable and
  the backend path is safe to run in the CLI process.

The preferred design is runtime-backed execution because it keeps session
binding, delivery, logging, cancellation, and backend environment handling in
one place.

Synchronous runs have no fixed default wait limit. If execution exceeds 30
minutes, the CLI returns an async accepted response, keeps the `agent_runs` row
running, and the user can continue through `vibe runs show/list`. The
30-minute threshold is a system protection threshold, not a user-visible default
timeout. `--wait-timeout` only changes how long the CLI waits; it does not mean
the run times out or stops automatically.

Agents may call `vibe agent run` again as a harness/sub-agent mechanism, but the
run metadata should record `parent_run_id`. A simple recursion guard should be
added before implementation:

- maximum nesting depth;
- cycle detection by parent chain;
- clear failure status when the guard blocks a run.

## Webhook Direction

Future webhook support should follow the same run creation path:

```text
external webhook -> validate source -> build RunSpec/message payload -> create agent_run
```

The concrete CLI can be designed later, but the schema should already leave room for
`source.kind=webhook` and structured `payload_json`.

## Specification Summary

1. `vibe hook send` should be deprecated and replaced by `vibe agent run`.
2. Agent Run message flags should be `--message` and `--message-file`, not `--prompt`.
3. Agent Run must support existing `--session-id`.
4. The shared storage model is `run_definitions` definitions plus
   `agent_runs` executions.
5. Run inspection and management live under `vibe runs`.
6. Synchronous `vibe agent run` has no fixed default wait limit; after 30
   minutes it continues asynchronously as a system protection threshold, and
   `--wait-timeout` only controls CLI waiting.
7. `vibe runs cancel` is best-effort; if the run finishes after cancellation was
   requested, preserve the real `succeeded` / `failed` terminal status and keep
   `cancel_requested=true`.
