# Agent Run Callback Session

Status: proposal.

## Background

`vibe agent run` lets one Agent queue async work that continues in the
background by default. The missing piece is a first-class way for the completed
run to return its full result to the Session that initiated or is waiting for
that work.

This is especially important for Agent-to-Agent delegation:

```text
caller Session -> async Agent Run -> target Session/scope
                         |
                         v
                 full result callback
                         |
                         v
                 caller Session continues
```

Terminology:

- **Target Session**: the Session where the async run executes.
- **Caller Session**: the Session that should receive the completed run result.
- **Callback**: a follow-up Agent message created from the completed run result
  and delivered to the Caller Session.

Avoid "parent Session" / "parent Agent" in user-facing naming because that can
confuse run lineage with message delivery.

## Product Semantics

Add a callback option to direct Agent Runs:

```bash
vibe agent run \
  --session-id <target-session-id> \
  --callback-session-id <caller-session-id> \
  --message "Run this delegated task."
```

Rules:

- `--callback-session-id` names the Caller Session.
- The async run still executes in its own Target Session or target scope exactly
  as it does today.
- When the async run reaches a terminal state, Avibe sends the full execution
  result as a new message to the Caller Session.
- Callback delivery is independent from ordinary output delivery. If the target
  run also posts to its IM scope, the callback still happens.
- Callback sends all terminal outcomes for v1: success, failure, cancellation,
  and terminal errors. Do not add filtering flags yet.
- The callback message should enter the Caller Session through the same
  scheduled/watch-style turn path, so it queues behind any active turn instead
  of interrupting it.
- The callback message should trigger the Caller Session Agent as a normal
  follow-up message. It is not just passive transcript decoration.

## Message Content

The callback message should contain only the completed run's final result text,
not a status notification or process transcript.

Priority:

1. Use `agent_runs.result_text` when present.
2. If the run failed and has no `result_text`, construct a failure result from
   `error`, `stderr`, and stdout when useful for diagnosing a failed run.
3. If the run was canceled and has no `result_text`, construct a cancellation
   result.
4. If there is truly no result content, skip sending an empty callback but
   persist the callback state as skipped.

Do not include wrapper metadata such as run id, status, agent name, target
session, system messages, tool calls, or intermediate assistant updates in the
callback body.

## CLI/API Shape

### CLI

Add to `vibe agent run`:

```bash
--callback-session-id <session-id>
```

Validation:

- v1 requires `--async` when `--callback-session-id` is passed.
- The callback session id must resolve to an active `agent_sessions` row.
- Archived sessions are invalid.
- Callback to the same Session is allowed but should be documented as a loop
  risk for automation authors.
- `--callback-session-id` is orthogonal to target Session selection and scope
  placement.

Output payload should include:

```json
{
  "callback_session_id": "ses...",
  "run": {
    "callback_session_id": "ses..."
  }
}
```

### Internal/API Payload

Carry `callback_session_id` through the same run-spec path as `session_id` and
scope placement, so CLI, future UI/API creation, and persisted run history do
not drift.

## Data Model

Add persisted callback fields to `agent_runs`.

Minimum v1 columns:

- `callback_session_id text null`
- `callback_status text null`
- `callback_error text null`
- `callback_run_id text null`
- `callback_completed_at text null`

Suggested callback statuses:

- `pending`: callback requested but not attempted.
- `sent`: callback run/message was queued successfully.
- `skipped`: no callback was sent because no callback was requested or there
  was no content to send.
- `failed`: callback dispatch failed.

Why explicit columns instead of only `metadata_json`:

- callback state is user-visible run state;
- it needs to be queryable in `vibe runs show/list`;
- retries/debugging should not depend on parsing opaque metadata.

Also update:

- SQLAlchemy model definition.
- Alembic migration.
- lightweight local migration path in `storage/migrations.py`.
- import/export or state-backfill paths that enumerate `agent_runs` columns.

## Execution Design

Callback should happen in the Harness execution layer after the target run is
terminal and after `result_text` has been recorded.

High-level flow:

1. `vibe agent run ... --callback-session-id ses_calling` enqueues an
   `agent_run` row with `callback_session_id=ses_calling` and
   `callback_status=pending`.
2. The scheduler/request drain executes the target run as today.
3. Terminal output is recorded on the target `agent_runs` row.
4. The Harness layer builds callback message content from the terminal run row.
5. The Harness layer enqueues a new callback Agent Run into the Caller Session
   using the same scheduled/watch Agent turn path.
6. The original run persists the callback update as `sent`, `skipped`, or
   `failed`, including `callback_run_id` when a follow-up run is created.

Important boundary:

- Do not implement callback in the CLI process. `--async` returns immediately,
  and callback must survive CLI exit and service restart.

## Turn/Queue Semantics

For Workbench/Avibe Sessions:

- callback enters `SessionTurnManager.submit_scheduled(...)`;
- if the Caller Session is busy, callback is queued;
- if idle, callback starts a normal scheduled-source turn;
- callback must not interrupt the active turn.

For IM-backed Sessions:

- callback uses the existing scheduled-message path and delivery context for
  that Session.

This mirrors watch follow-up behavior and keeps callback behavior consistent
with existing Harness semantics.

## Failure Handling

The target run can succeed while callback fails. These are separate outcomes.

Run status remains the target execution result:

- target succeeded + callback failed => run status `succeeded`,
  `callback_status=failed`.
- target failed + callback sent => run status `failed`, callback still records
  `sent`.

Retries:

- v1 can be manual: `vibe runs show <run-id>` exposes callback failure details.
- A later `vibe runs callback <run-id>` or `vibe runs retry-callback <run-id>`
  can be added if needed.

## Tests

Focused test coverage:

- CLI parser accepts `--callback-session-id` for default-async direct Agent Runs.
- CLI payload and persisted row include `callback_session_id`.
- unresolved/archived callback Session is rejected.
- completed successful run dispatches full `result_text` into Caller Session.
- failed run without `result_text` dispatches constructed failure content.
- target IM delivery and callback both happen when both are configured.
- busy Workbench Caller Session queues callback instead of interrupting.
- callback failure does not overwrite target run status.
- `vibe runs show` includes callback state.

## Documentation

Update:

- CLI docs for `vibe agent run`.
- Chinese CLI docs.
- `skills/use-avibe/SKILL.md` if command guidance mentions async Agent Runs.

Docs should use "Caller Session" / "调用方 Session" consistently.

## Non-Goals For v1

- No `--callback-on success|failure|always` filter.
- No result summarization/compression.
- No callback retry command unless implementation reveals the need.
- No special "parent run" behavior.
- No automatic loop prevention beyond normal session serialization.

## Implementation Sequence

1. Add schema/model/store support for callback fields.
2. Extend `TaskExecutionRequest` and `enqueue_agent_run(...)` to carry
   `callback_session_id`.
3. Add CLI flag, validation, and JSON output.
4. Add callback message builder from terminal run rows.
5. Dispatch callback through the scheduled/watch-style Session path.
6. Expose callback state in `vibe runs show/list`.
7. Add focused unit tests.
8. Update CLI docs and Avibe skill docs.
