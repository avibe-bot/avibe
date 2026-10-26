## Agent Harness: Runs, Tasks, and Watches

Use the harness commands when the user wants an agent to leave the current turn and continue later, repeatedly, or in the background. The mental model is:

- `vibe agent run`: run one concrete Agent job now. Runs are async by default; pass `--sync` only when the current CLI must wait for the result.
- `vibe task`: save a time trigger that creates Agent Runs later
- `vibe watch`: save a condition trigger that waits for a process, file, log, CI, review, or other signal, then creates a follow-up Agent Run
- `vibe runs`: inspect and cancel concrete run records

Agents should prefer these managed harness commands over ad-hoc detached shells when the work should be inspectable, resumable, or report back to the conversation.

Preferred CLI shape:

- delegate to another Agent in a private/background Session from an Avibe Agent shell: `vibe agent run --agent '<agent-name>' --message '...'`
- delegate to a visible sibling Session in the same scope from an Avibe Agent shell: `vibe agent run --agent '<agent-name>' --same-scope --message '...'`
- wait for an Agent result in the terminal: `vibe agent run --sync --agent '<agent-name>' --message '...'`
- continue a specific existing Session: `vibe agent run --session-id '<session-id>' --message '...'`
- queue a new message behind the active Turn instead of steering it: `vibe agent run --session-id '<session-id>' --queue --message '...'`
- steer an already-queued exact head without adding a message: `vibe session send-now --session-id '<session-id>'`
- inspect another Session's durable FIFO queue: `vibe session queue list --session-id '<session-id>'`
- remove one exact queued message after inspecting its stable ID: `vibe session queue remove '<message-id>' --session-id '<session-id>'`
- fork this Session for an alternate path: `vibe agent run --fork-self --message '...'`
- fork another explicit Session for an alternate path: `vibe agent run --fork-session '<source-session-id>' --message '...'`
- recurring task for this conversation: `vibe task add --cron '<expr>' --message '...'`; write cron weekdays as names (`mon` ... `sun`), since numeric weekdays are rejected
- one-off task for this conversation: `vibe task add --at '<ISO-8601>' --message '...'`
- task that creates a visible sibling Session: `vibe task add --create-session --same-scope --cron '<expr>' --message '...'`
- scheduled command with no Agent turn: `vibe task add --cron '<expr>' --shell '<command>'`
- command task with AI failure triage: `vibe task add --cron '<expr>' --shell '<command>' --on-failure agent --message '<what to do>'`
- immediate rerun: `vibe task run <id>`
- managed background watch for this conversation: `vibe watch add --message '...' -- <cmd>` (or `--shell '<cmd>'` to pass a single shell string)
- watch that creates a visible sibling Session: `vibe watch add --create-session --same-scope --message '...' -- <cmd>`
- update a watch: `vibe watch update <id> --name '...' --timeout 1200`
- inspect a run: `vibe runs show <run-id>`; add `--brief` to print only its `vibe runs list` row
- cancel a run: `vibe runs cancel <run-id>`

Targeting and callbacks:

- In an Avibe-injected Agent shell, commands that operate on this conversation default to the current Agent Session. Omit the target when the work should continue here.
- Use `--session-id <id>` only when the command should operate on a different existing Agent Session.
- When `vibe agent run --session-id <id>` targets an existing Session, it sends a new message into that Session. It does not change that Session's cwd, scope, Agent, model, or reasoning settings.
- When coordinating another Workbench Session, decide whether its current turn should finish or be preempted from the work dependency, urgency, and cost of discarding in-flight work. An explicit user request is one signal, not a prerequisite.
- A new message to an existing Session is P1 by default: it steers into an active Turn, starts when idle, and falls back to P3 only after a definitive refusal; it never promotes an older queued message. Add `--queue` to put it in P3 behind the active Turn instead.
- Use `vibe session send-now --session-id <session-id>` when a Session already has queued work and no new message should be added. This promotes the existing FIFO head.
- Both commands use the shared steering path for Workbench and IM Sessions. A refused or stale steer leaves the affected input durably queued and never falls back to Stop.
- Use `vibe session queue list --session-id <session-id>` before changing another Workbench Session's queue. If an instruction is obsolete, contradictory, or duplicated, remove that exact stable row with `vibe session queue remove <message-id> --session-id <session-id>`. Never guess a message ID or delete another row to simulate reordering.
- When `vibe agent run` creates a new Session, the default placement is private/background. Add `--same-scope` for a visible sibling Session in the same Workbench project or IM scope, or `--scope-id <scopes.id>` for a specific existing scope.
- When a task, watch, or new Agent run creates a Session and `--cwd` is omitted, Avibe uses the command's current working directory. Forks keep the source Session cwd by default.
- Async Agent runs return the final result to this conversation by default. Pass `--no-callback` only when you will inspect the run later with `vibe runs`. Pass `--callback-session-id <id>` only when the final result should return to a different Session.
- `--message` and `--message-file` are the user-message flags for task, watch, and agent-run commands.
- `vibe task add` stores a message template — or, with `--shell` / a trailing `-- <argv>`, a command — and creates Runs when the time trigger fires. A command task runs with no Agent turn: silent on success, a failure notice on failure, `--on-failure agent` to escalate failures to an Agent.
- `vibe watch add` uses `--message` as the instruction template for the Agent Run created after the waiter reaches a reportable state.
- `--fork-self` forks this Session's native backend context. `--fork-session <id>` forks another explicit Session. Forks keep the source Session backend, scope, and cwd by default.
- `--model` and `--reasoning-effort` set the model of any new Session a run creates (blank or a fork); the Session keeps it, and they are refused with `--session-id`. Fork overrides stay narrow: `--agent`, `--model`, and `--reasoning-effort` can override the forked Session only if the backend stays the same. Do not combine fork flags with `--session-id`.

Operational guidance:

- use `vibe task list` before editing or deleting an existing task; use `vibe watch list` before touching a managed watch
- `vibe harness status`, `vibe runs list`, `vibe task list`, `vibe watch list`, and `vibe show list` read this Session by default; pass `--session-id <id>` for another Session or `--global` for every Session
- if this is the first time using `vibe task add`, `vibe agent run`, `vibe runs`, or `vibe watch add`, read the matching `--help` output first — watches and command tasks both accept `--shell` and `--timeout` (per-cycle for watches, per-run for tasks), while `--lifetime-timeout` (overall), `--forever`, `--retry-exit-code`, and `--retry-delay` stay watch-only
- for Watch waiters, exit `0` means one new reportable event; an allowed retry exit code keeps either mode waiting, while `64` plus `avibe-watch: no-event` ends a once Watch or re-arms a forever Watch without an Agent Run; a once waiter that is still waiting must use a retry exit code
- a forever waiter must keep a durable cursor, state transition, or domain cooldown; Avibe serializes its event follow-ups and automatically pauses plus sends a repair Run after six successful events within 60 seconds
- use `vibe task update <id>` to keep the same task ID while changing name, schedule, message, agent, or target
- use `vibe watch update <id> ...` when you must rename, retarget, or change the waiter/options
- Agent-facing collection commands return 20 compact rows per page, cap `--limit` at 100, and have no unpaginated `--all` mode; follow `pagination.next_command` when more rows exist
- list rows omit null, empty, and zero-valued fields, so a missing key means unset; `--brief` on `vibe task show`, `vibe watch show`, `vibe runs show`, `vibe session get`, or `vibe agent show` prints exactly the list row
- find a task or watch by text with `vibe task list --q <text>` or `vibe watch list --q <text>`; enabled definitions come first, newest activity first
- `vibe agent run` and `vibe task run` print one `run` object; read the Run id from `run.id`
- read lifecycle from `lifecycle_state`; `health` appears only when it is not `healthy`, and a watch's configured retry exit shows as `last_exit_code` with no `last_error`
- read task/watch records from `definition` for detail commands and `definitions` for list commands; command-specific duplicate aliases are not emitted
- use `--include-finished` for paginated task/watch one-shot history
- both list commands hide successful one-shot definitions by default while failures stay visible; use `--include-finished` and follow `pagination.next_command` for bounded history
- use `vibe task show <id>`, `vibe watch show <id>`, or `vibe runs show <run-id>` to inspect stored fields and runtime state
- use `vibe task pause` / `vibe task resume` and `vibe watch pause` / `vibe watch resume` to disable a task or watch without deleting it
- treat `warnings` from task, watch, agent-run, or runs commands as delivery-risk hints to fix proactively
