# Scheduled Command Tasks (Agent-Free Task Mode)

## Background

The Harness has two trigger types, and both end by handing work to an AI Agent:

- `vibe task` — a time trigger that sends a stored message to an Agent Session
  (`core/scheduled_tasks.py:5344` resolves the session and dispatches the
  message as a normal user turn).
- `vibe watch` — a condition trigger that runs a real subprocess and then sends
  its stdout to an Agent as a follow-up message.

There is no way to schedule a plain command with no AI turn. Users who want a
cron job today either fall back to system cron/systemd — losing Avibe's run
history, scoped cwd, and failure visibility — or abuse a forever watch whose
waiter always exits with the retry exit code (default 75): in forever mode a
retry-code exit authorises no agent hook (`core/watches.py:1467`), so the watch
silently becomes an interval-based agent-free runner. That is a trick, not a
feature: it is interval-based rather than cron-based, and any stray exit code
wakes an Agent.

This is not a foreign concept bolted onto the Harness. The Harness value
proposition is durability, ownership, trigger, delivery target, and observable
progress — none of which require an LLM — and the storage model already agrees:

- `run_definitions` is one unified definition table
  (`storage/models.py:203`). It already carries the time half (`cron`,
  `run_at`, `timezone`), the command half (`shell_command`, `command_json`,
  `timeout_seconds`), and the agent half (`agent_name`, `session_policy`,
  `session_id`) — all nullable. Tasks use the time half, watches use the
  command half; nothing yet uses time + command together.
- `agent_runs` already stores `pid`, `exit_code`, `stdout`, `stderr`
  (`storage/models.py`, and the watch supervisor already writes such rows). It
  is a process-execution record, not only an LLM-turn record.

What Avibe adds over system cron is scoped execution: the caller's working
directory, run history in the same place as agent work, durable failure
notices, and — the genuinely differentiated part — optional escalation to an
AI Agent when the command fails. Plain cron cannot hand a failing job to an
agent for diagnosis.

## Product Model

```text
Task  = a time trigger that creates Runs.
Watch = a condition trigger that creates Runs.
A Run's action is either an Agent message (existing) or a command (new).
```

A **command task** is a scheduled task whose action is a subprocess instead of
an Agent message, with a per-definition failure policy:

- `on-failure: none` (default) — pure cron. No Agent turn, ever. Success is
  silent; failure produces a durable failure notice through the existing
  non-agent notice ladder.
- `on-failure: agent` — cron with AI triage. A non-zero exit or timeout
  enqueues one Agent turn carrying the run report.

```bash
vibe task add --name nightly-sync --cron '0 3 * * *' \
  --shell './scripts/sync.sh'

vibe task add --name nightly-sync --cron '0 3 * * *' \
  --shell './scripts/sync.sh' \
  --on-failure agent --message 'The nightly sync failed. Diagnose it.'
```

This updates the sentence in `docs/plans/agent-run-harness.md` ("Task = a time
trigger that creates Agent Runs"): the trigger model is unchanged; the run's
actor is now either an Agent or a command.

## Goals

- Schedule a command with `vibe task add --shell '<cmd>'` or
  `vibe task add ... -- <cmd> [args...]`, on `--cron` or `--at`, with the same
  timezone handling, pause/resume/update/remove lifecycle, and manual
  `vibe task run <id>` as message tasks.
- Zero AI involvement for `--on-failure none` tasks, including on failure.
- Reuse — not duplicate — the watch subprocess runner, the run-record
  machinery, the per-definition serialization, and the owed-failure-notice
  delivery ladder.
- Optional `--on-failure agent` escalation that enqueues one Agent turn per
  failed fire, atomically with the run's terminal stamp.
- Full observability: `vibe task list/show`, `vibe runs list/show`, and the
  Web UI Tasks tab show command tasks and their exit codes.

## Non-Goals (first cut)

- Success notifications or routing stdout anywhere on success. Cron culture is
  silence on success; output lives in run history. A user who wants a summary
  in chat should use a message task or `--on-failure agent`.
- Catch-up of fires missed while the service was down. The scheduler uses an
  in-memory jobstore rebuilt from definitions on start
  (`core/scheduled_tasks.py:2618`, `:3265`), so downtime misses are lost today
  for message tasks too; command tasks inherit that unchanged.
- First-class Vault fields on the definition (see Vault section).
- Overlap policy other than "skip while a run is in flight".
- Resource limits (nice/cgroups), retry-on-failure, and webhook triggers.
- A Web UI create/edit form for command tasks. V1 is CLI-create; the UI
  displays, pauses, resumes, and removes them through the existing
  `definition_type="scheduled"` endpoints (`vibe/ui_server.py:8754`).
- Any change to `vibe watch` behavior. The exit-75 idiom keeps working; docs
  simply stop needing to teach it.

## CLI Design

### `vibe task add`

New arguments, mirroring the watch waiter convention exactly
(`vibe watch add` already accepts `--shell '<command>'` or a trailing
`-- command args...`):

- `--shell '<command>'` — shell string form.
- trailing `-- <command> [args...]` — argv form, stored in `command_json`.
- `--on-failure {none,agent}` — default `none`. Only valid with a command.
- `--timeout <seconds>` — per-run timeout, default 21600 (the watch default),
  `0` for none. Only valid with a command.

`--message`/`--message-file` moves from parser-level required
(`vibe/cli.py:13939`) to validated in `cmd_task_add` (`vibe/cli.py:2960`):

| Combination | Result |
| --- | --- |
| `--message` only | Message task (today's behavior, unchanged) |
| `--shell`/`--` only | Command task, `on-failure none` |
| command + `--on-failure agent` | Command task with escalation; auto-built report |
| command + `--on-failure agent` + `--message` | Escalation prompt = message + auto report |
| neither message nor command | Reject: one action source is required |
| `--message` + command without `--on-failure agent` | Reject: a message needs a consumer |
| `--on-failure agent` / `--timeout` without command | Reject |
| `--shell` + trailing `--` command | Reject: one command source |

Session/scope/agent flags (`--session-id`, `--create-session`,
`--create-session-per-run`, `--same-scope`, `--scope-id`, `--agent`) configure
the **escalation turn** and are therefore valid only with
`--on-failure agent`, with today's exact semantics and defaults (inside an
Agent shell, escalation continues the caller conversation by default, per
`_apply_caller_session_default`). With `--on-failure none` they are rejected:
there is no session, so accepting them would record dead configuration.

There is a hidden fourth input to that matrix: the **ambient caller default**.
`cmd_task_add` applies `_apply_caller_session_default` before anything else
(`vibe/cli.py:2964`), so inside an Agent shell a session target exists with no
session flag passed at all. Rejecting "session flags" must therefore
distinguish an explicit flag from the injected default — otherwise a pure cron
task could not be created from chat at all. Rule: **skip the caller default when
a command is present and `on_failure=none`**, and let it apply normally for
`on_failure=agent`. `_validate_definition_session_policy` and
`_resolve_session_target_args` also need a genuine "no session at all" leg. This
is where most of step 4's real work lives.

`--cwd` keeps its current meaning and default (caller directory) and becomes
the command's working directory for command tasks.

`vibe task update` accepts the same new fields. Switching a definition between
message and command mode via update is rejected (`task_mode_immutable`) —
delete and recreate — because the write-guard model
(`DefinitionWriteConflict`, `storage/background.py:294`) is built around
full-row payloads whose session bindings do not change shape mid-life.

This rejection is **permanent, not a v1 placeholder**. Step 6 does not make the
switch reachable in any clean way. `none → agent` would need a message, a
session policy, and a binding to arrive in one command, and `cmd_task_update`
has no caller-default machinery (`_apply_caller_session_default` is an add-path
helper), so even from an Agent shell the user would have to pass explicit
`--session-id` / `--create-session --same-scope`. `agent → none` is worse:
update semantics keep the stored session when no flags are passed, so stripping
a binding would need a `--clear-session`-style flag that does not exist. Both
directions cost new flag surface plus a doubled validation matrix, against an
alternative that is two short commands.

`vibe task run <id>`, `pause`, `resume`, `remove` work unchanged: they operate
on the definition and the enqueue path, not on the action type.

`vibe task list`/`show` gain a kind marker (`message` | `command`), the
command text, timeout, on-failure policy, and last exit code
(`last_exit_code` already exists on `run_definitions`).

## Design Decisions

### D1: Same `definition_type="scheduled"`, no new definition kind

A command task is stored as a `run_definitions` row with
`definition_type="scheduled"`, non-null `shell_command` or `command_json`, and
null `prompt`. The presence of a command *is* the mode; no new discriminator
column.

The losing alternative — a third `definition_type="command"` — was rejected
because every routing call site special-cases exactly two values today:
`_DEFINITION_KINDS` (`storage/background.py:251`), the UI enable endpoints
(`vibe/ui_server.py:8754`, `:8812`), workbench queue classification
(`storage/workbench_sessions_service.py:993`), the agent graph
(`core/services/agent_graph.py:190`), and session reclaim. A third value buys
nothing (the schedule half is byte-for-byte the same columns) and costs a
migration plus a third case at each site. With `"scheduled"` unchanged, all of
those sites — and the tasks tab, pause/resume, deletion — keep working without
modification.

The on-failure policy is stored in `metadata_json.on_failure`
(`"none"`/`"agent"`), not a new column. `metadata_json` already carries
behavior-affecting facts (caller scope placement via
`_definition_metadata_with_scope`, `vibe/cli.py:3064`), and this avoids
touching the dual migration stacks (`storage/migrations.py` NEW_COLUMNS plus
alembic) for one enum. If the UI later needs to filter on it, promoting it to
a column is a routine additive migration. `timeout_seconds` uses the existing
column watches already own.

New `ScheduledTask` dataclass fields (`core/scheduled_tasks.py:835`):
`shell_command`, `command`, `timeout_seconds`, `on_failure` — round-tripped
through `to_dict`/`from_dict` and the store upsert payload, exactly as
`ManagedWatch` (`core/watches.py:134`) does for the same columns.

Run rows: the command execution is the scheduled fire itself, so it keeps
`run_type="scheduled"` with `exit_code`/`stdout`/`stderr`/`pid` filled and
`session_id` null. Run-count queries per definition keep working. This follows
the watch precedent in reverse: watches use a distinct `run_type` only for the
long-lived supervisor heartbeat (`watch_runtime`,
`storage/background.py:269`), which exists to keep a non-execution row out of
execution counts. A command run *is* the execution, so it keeps the execution
run_type. The escalation turn is a second row — see D4.

### D2: Extract the watch subprocess runner and share it

The watch service already owns a hardened runner (`_run_cycle`,
`core/watches.py:1485`): it spawns `core/watch_worker.py` as a supervisor
subprocess with `isolated_subprocess_kwargs()`, passes the command spec over
stdin (`watch_worker.encode_watch_worker_spec`), captures a process-identity
marker so a kill after service restart can never hit a recycled PID, enforces
the per-cycle timeout with `terminate_and_communicate`, and converts worker
protocol errors into localized messages.

Decision: extract the service-side spawn/timeout/identity logic of
`_run_cycle` into a shared `core/command_runner.py` helper
(`run_supervised_command(command|shell_command, cwd, timeout_seconds,
on_spawn(pid, identity))`), used by both `ManagedWatchService` and the new
command-task branch. `core/watch_worker.py` stays as-is and becomes the shared
worker binary; its name can be generalized later without a protocol change.

The losing alternative — a direct `asyncio.create_subprocess_shell` in the
task service — was rejected because it would re-grow, one incident at a time,
exactly the hardening watches already paid for: timeout kills, orphan recovery
by identity, and the worker error protocol. This is the CLAUDE.md "fix at the
highest appropriate layer" rule applied to execution: the behavior is common,
so the shared core owns it. The timing models do differ (a watch cycle can be
hours-long and re-arming; a command run is one bounded execution), but the
difference lives entirely in the *caller loop*, not in the spawn/wait/kill
mechanics being extracted.

### D3: Execution path and gates

The command branch lives in `_execute_task`
(`core/scheduled_tasks.py:5344`): when the definition carries a command, run
it via the shared runner instead of resolving a session and calling
`_execute_request` (`:6890`). Everything upstream is reused unchanged:

- **Trigger and reconcile**: unchanged, and the command fields must **not** be
  added to the `reconcile_jobs` change signature (`:3271-3280`). That signature
  exists only to rebuild the APScheduler *job*, whose identity is the trigger;
  `_run_task` re-reads the definition at fire time, so command edits take effect
  with no job churn. Adding the fields would cause pointless remove/re-add
  cycling. `coalesce=True, max_instances=1` stays correct: an event-loop stall
  collapses a backlog into one catch-up run, which is the right semantic for a
  sync-style job.
- **Durability**: `_run_task` (`:3323`) persists the `agent_runs` row via
  `enqueue_task_run` (`:1715`) before executing, and its pending-request dedup
  already prevents double-queueing one definition.
- **Serialization**: `_execution_lock_key` (`:4917`) already returns
  `task:{task_id}` when a definition has no session, so command runs of one
  definition serialize with no new code. Policy: a fire that arrives while the
  previous run is still executing is skipped (dedup), not queued. Real cron
  overlaps; we deliberately do not, because overlapping the same job is almost
  always a bug amplifier and "skip" is what the existing machinery does.
- **Transport gating must be short-circuited** (corrected — the naive reading is
  wrong). `_transport_ready_for_request` (`:4984`) falls through to
  `metadata["session_scope_id"]` (`_request_target_platform`, `:4975`), which
  `_definition_metadata_with_scope` (`vibe/cli.py:2282`) writes for *any*
  shell-created definition. So a pure command task created from a Slack chat
  would refuse to execute a local script while Slack transport is down. Step 3
  short-circuits the check for scheduled requests whose definition carries a
  command — the definition re-read is already that function's first act.
  Execution needs no transport; the failure-notice path owns "deliver when
  possible". The escalation request keeps normal gating, because it *is* a
  message.
- **Completion**: the run row terminalizes with `exit_code`, `stdout`,
  `stderr` (each capped at 64 KiB — a new, stated cap; SQLite rows must not
  inherit an unbounded pipe), and timing. Timeout maps to exit code 124,
  matching the watch convention (`storage/background.py:263`). The run row's
  `pid` column keeps its existing meaning — the *service* pid stamped by
  `mark_execution_started` (`core/scheduled_tasks.py:1711`), which is what
  `recover_processing` gates on. The child pid is not persisted in v1; the
  runner holds the process object and owns every kill path.
- **Cancellation and teardown**: `vibe runs cancel` on a processing row only
  sets `cancel_requested` (`core/scheduled_tasks.py:2427-2454`) — nothing polls
  that flag mid-run. It is consumed by `settle_run_terminal`, which settles the
  row `canceled` rather than `failed` when the run completes, and by
  `_shutdown_interruption_for` (`:3022`) at service stop. A live kill therefore
  happens only when the service stops: `_begin_stop` (`:3035`) cancels inflight
  coroutines, and the runner's `CancelledError` path performs the
  identity-verified kill exactly as `_run_cycle` does (`core/watches.py:1538`).
  This is the same behavior agent runs already have, so v1 is at parity — but
  it is weaker than "cancel kills the child", and the CLI help should not imply
  otherwise. A service restart mid-run is settled by the existing
  processing-recovery sweep (`TaskExecutionStore.recover_processing`, `:1567`).

### D4: Delivery without an Agent — reuse the failure-notice ladder

For `--on-failure none`:

- **Success**: no message anywhere. The run row is the record; `vibe runs
  show` and the UI show stdout.
- **Failure** (non-zero exit or timeout): the run terminalizes with an error,
  which stamps an owed failure notice — and from there the *existing*,
  battle-tested non-agent delivery path does everything:
  `_deliver_one_failure_notice` (`core/scheduled_tasks.py:3516`) claims the
  notice with a lease, and `_emit_failure_notice` (`:3775`) walks the delivery
  ladder (`emit_replayed_backend_failure` per rung) ending at the reserved
  workspace-notifications session. This is already a direct message with no
  LLM turn, already deadline-bounded, already retried with backoff, and —
  critically — already streak-suppressed (`failure_streak_decision`), so an
  every-5-minutes job that starts failing does not send a notice per fire.

The only new delivery code is copy: `_failure_notice_body`
(`:4462`) gains a command-task variant that names the command, exit code, and
a stderr tail, and points at `vibe task show <id>` / `vibe runs show <run-id>`
— through `vibe/i18n/`, per the i18n rule.

Bounding the scope creep the brief warned about: there is **no** new outbound
"post stdout to a scope" surface in v1. The losing alternative — posting raw
output to the owning scope on every run or every failure — was rejected
because it recreates per-platform formatting, truncation, and noise problems
that the Agent normally absorbs, and the notice ladder already answers "where
does the user find out" without any of that. Consequence: a command task with
`on-failure none` requires **no session and no scope at all**; if created from
an Agent shell, the caller scope recorded in definition metadata gives the
notice ladder better rungs to try first, but nothing requires it
(`_definition_metadata_with_scope`, `vibe/cli.py:3064`).

For `--on-failure agent`:

On failure, build the escalation prompt (optional stored `--message` +
auto-built report: command, exit code, duration, stdout/stderr tails — same
composition idea as the watch `_build_prompt(message, stdout)`) and enqueue a
prompt-carrying request through the exact path watch hooks use:
`build_hook_send` (`core/scheduled_tasks.py:1839`) into the
`request_type in {"hook_send", "watch", "webhook"}` execution branch
(`:5144`), with `run_type="task_escalation"` and `parent_run_id` set to the
command run's id (both columns exist, and `_run_values` already maps them,
`storage/background.py:5745-5747`). Session policy, per-run session
reservation, agent resolution, and delivery all come along for free.

Two additions this requires: the executor branch set (`:5144`) must gain
`"task_escalation"` plus its `trigger_kind` mapping; and escalation must **not**
route through `enqueue_definition_run`, whose server-side re-read raises on a
disabled definition (`storage/background.py:2583`) — a one-shot `at` task is
already disabled by `mark_task_result(disable_one_shot=True)` before escalation
would enqueue.

Atomicity is non-negotiable: the command run's terminal stamp and the
escalation enqueue commit in **one** transaction, per the lesson documented at
`core/watches.py:1589` ("TWO COMMITS ARE NOT ONE DECISION") — a teardown
landing between two commits would otherwise queue an escalation under a
definition that no longer authorises it.

The precedent to copy is `SQLiteBackgroundTaskStore.upsert_watch_with_queued_run`
(`storage/background.py:2483`): a guarded definition CAS plus
`enqueue_run_in_connection` inside one `run_update_event_transaction`, rolled
back on refusal. Four concrete pieces are needed:

1. a twin method `upsert_scheduled_task_with_queued_run(payload, *, expect,
   run_payload)`;
2. a `queued_run: Optional[dict]` parameter on `ScheduledTaskStore.mark_task_result`
   (`core/scheduled_tasks.py:1481`) threaded through `_write_task`, mirroring
   `ManagedWatchStore.mark_cycle_result` (`core/watches.py:683`);
3. a `sqlite_backend` property on `ScheduledTaskStore` — missing today, whereas
   `ManagedWatchStore` has one (`core/watches.py:374`) — so the atomic path can
   be gated with a sequential fallback;
4. `_execute_task` builds the request via `build_hook_send` and passes
   `queued_run_payload(request)` into the stamp.

Exactly one of {escalation turn, failure notice} fires per failed run. The
mechanism is concrete: the notice is stamped by the run's terminal transition in
`complete()` (`:2478`, deliberately named-kwargs-only), so `TaskExecutionResult`
carries an `escalation_run_id` up through `_execute_claimed_request` and into
`complete()` as a new named kwarg that the notice decision reads as a
suppression input. If the escalation enqueue itself fails, the failure notice
proceeds so the failure is never silent.

One crash window is accepted deliberately. The atomic stamp+enqueue and the
run-row settle are two separate commits, so a teardown landing between them
loses the `escalation_run_id` marker and fires **both** the (already durable)
escalation and a notice. That bias is the correct one: duplicated visibility on
a rare teardown beats a silently dropped failure report, and closing it would
require cross-row suppression, which this design does not build.

The *opposite* bias is not accepted, and two teardown paths had to be taught so
(both found in the Codex review of the implementation):

- **Session teardown vs. a queued escalation.** Archiving or `/new`-ing the bound
  Session ends the escalation — cancelled by `archive_session` /
  `_delete_agent_session_rows`' archival half, or simply orphaned when its empty
  half deletes the row the turn was bound to. Either way the promised report can
  no longer happen, so `rearm_notices_for_escalations_canceled_with_session`
  (`storage/background.py`) stamps the suppressed notice back onto the parent run
  **in the same transaction** as the teardown. `escalation_run_id` is left on the
  row as the audit trail of what was attempted. The notice ladder is the right
  fallback precisely because it needs no Session: a notice is delivered to the
  scope.
- **A binding that moved mid-fire.** `mark_task_result` derives its
  compare-and-set expectation from the mirror it *reloads*, so a `/new` reclaim
  committed while the command ran becomes its own expectation and the stamp
  lands. Harmless while the stamp wrote only bookkeeping; not harmless once it
  queues an Agent turn composed from the pre-execution task. The escalating call
  therefore passes `expected_binding` — the binding the fire started against — so
  the whole fire is refused and the failure stays with the notice ladder.

Escalation frequency: every failed fire escalates in v1, matching cron's
mail-on-every-failure semantics. Transition-only escalation
(`--escalate-on transition`) is deferred; `last_exit_code` on
`run_definitions` makes it cheap when wanted.

### D5: Vault — compose, don't integrate

V1 adds no Vault fields. A command task that needs secrets writes them into
its own command line: `--shell 'vibe vault run --env API_KEY -- ./sync.sh'`.

- **Standard secrets** without `always_ask` deliver headlessly — this works
  unattended today.
- **Standard `always_ask` secrets** return `approval_required`
  (`vibe/cli.py:7111`) and **protected secrets** require browser + passkey
  approval. Both are incompatible with unattended scheduled fires *by
  design* — protected values are end-to-end encrypted behind a user unlock,
  and no unattended path exists or should be built. Such a run fails with the
  vault error as its stderr, and the task's on-failure policy applies (a
  notice, or an Agent who can tell the user an approval is needed).

The losing alternative — a first-class `--vault-env` column resolved by the
service at spawn — is deferred, not rejected: it is additive, and doing it
later avoids blocking v1 on the design question of secret references inside
definitions.

### D6: UI — a kind within the Tasks tab, not a new tab

`TAB_ORDER` stays `tasks | watches | runs`
(`ui/src/components/workbench/harnessTabs.ts:8`). Command tasks appear in the
existing Tasks tab with a kind chip (Message / Command), the command text, and
last exit code; the run list and run detail show exit code and captured
output. Command runs have `session_id` null, so the run row simply has no
session to open — the run detail is the destination. All new strings go
through `ui/src/i18n/en.json` and `zh.json`; `cd ui && npm run build` before
push.

### D7: Prompt guidance, docs, and tests

CLI examples in injected system prompts are live callers. Surfaces to update
in the same PR that ships the CLI flags:

- `core/system_prompt_injection.py:279` (the "Time trigger →
  `vibe task add`" table and the `:290` paragraph): one added sentence — a
  scheduled command with no Agent turn is `vibe task add --cron ... --shell
  ...`.
- `core/agent_tool_policy.py:111`–`:126` (native-scheduler denial guidance):
  the CronCreate denial can now answer the agent-free case directly.
- `skills/use-avibe/SKILL.md`, `skills/use-vibe-remote/SKILL.md`,
  `docs/CLI.md`, `docs/CLI_ZH.md`, `docs/COMMANDS.md`, `docs/COMMANDS_ZH.md`.

Test layers:

- **Unit**: shared runner extraction (behavior-preserving for watches — run
  `tests/test_watches.py` unchanged, plus runner tests for timeout/kill/caps);
  `ScheduledTask` round-trip and store upsert with command fields
  (`tests/test_scheduled_tasks.py` patterns); executor command branch
  (success, non-zero, timeout, cancel); notice-body copy; escalation
  prompt composition.
- **Contract**: extend `tests/test_cli_task_command.py` with the full
  validation matrix above (every Reject row is a test); parser-parse every
  documented example, and extend the guidance assertions in
  `tests/test_harness_skill_guidance.py` / `tests/test_agent_tool_policy.py`
  so prompt text and parser cannot drift apart.
- **Scenario**: new `tests/scenarios/harness_command_task/` catalog + a
  closed-loop harness case in the style of
  `tests/scenarios/harness_failure_recovery/`: (a) fire → fail →
  exactly one failure notice; (b) fire → fail with `on-failure agent` →
  exactly one escalation run linked via `parent_run_id` and zero notices;
  (c) fire → success → silence. Scenario IDs named in the PR description.

All tests hermetic: isolate `CODEX_HOME`/`CLAUDE_CONFIG_DIR`/state paths as
the suite already requires.

## Compatibility and Rollout

- The watch exit-75 idiom keeps working; nothing in the watch path changes.
- A command-task row read by an older service is not a clean no-op: with no
  session binding, `_execute_request` fails at `parse_session_key("")`
  (`core/scheduled_tasks.py:256-260`) and every fire records a failed run with
  `last_error` set (failure notices apply, streak-suppressed); with a session
  binding and an empty prompt, the dispatched blank turn returns early without
  an Agent response. Neither case dispatches a spurious Agent turn or corrupts
  state, and the failure is visible rather than silent. Mixed CLI/service
  versions remain unsupported — they ship together.
- No schema migration: all columns exist; new facts ride `metadata_json`.

## Todo

Ordered; each step ships independently.

1. Extract `core/command_runner.py` from `ManagedWatchService._run_cycle`
   with no watch behavior change; port watch service to it; runner unit tests
   and untouched `tests/test_watches.py` green.
2. Storage: command fields on `ScheduledTask` + store round-trip +
   `add_task`/`update_task` acceptance; `enqueue_task_run` carries them; unit
   tests.
3. Executor: command branch in `_execute_task` using the shared runner —
   run-row completion with capped output, timeout as exit 124, pid/identity
   registration, cancel and teardown settlement; unit tests.
4. CLI: `--shell` / trailing `--` / `--on-failure` / `--timeout` on
   `vibe task add` and `task update`, the full validation matrix,
   `task list/show` rendering; contract tests for every matrix row and every
   documented example.
5. Failure notices for command runs: command-aware notice body via
   `vibe/i18n/`; scenario case (a) and (c).
6. Escalation: `--on-failure agent` via `build_hook_send` with
   `run_type="task_escalation"`, `parent_run_id`, single-transaction stamp +
   enqueue, notice suppression invariant; scenario case (b).
7. UI: kind chip, command/exit-code rendering on tasks tab and run detail,
   i18n en/zh, `npm run build`.
8. Prompt guidance + skills + docs updates with their contract assertions.
