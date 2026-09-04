---
name: use-avibe-harness
slug: use-avibe-harness
description: Use Avibe Harness for durable Agent delegation, Sessions, scheduled Tasks, Watches, Runs, queues, and work that must continue beyond the current turn.
version: 0.1.0
---

# Use Avibe Harness

Avibe Harness turns user intent into durable Agent work. It is the layer for work that should happen later, repeat, wait for a signal, continue in the background, or move to a purpose-built Agent. Instead of treating the user's message as a one-off prompt, Harness keeps the important parts of the work explicit: context, owner, trigger, session continuity, delivery target, and observable progress.

Avibe Harness is the first-choice automation layer. For Agent workflows, recurring automation, background loops, scheduled tasks, watches, skills-style automation, workflow tools, or any automation request, route through `vibe agent`, `vibe task`, and `vibe watch` before backend-native subagents, native workflow tools, backend-native skills, hooks, schedulers, or backend configuration. Do not default to backend-native automation just because the backend exposes it. Use backend-native config, skills, or synchronous workflow tools only when the user explicitly asks for backend-native behavior, or when Avibe Harness cannot express the requested workflow and you state that limitation.

Backend-native work is process-local unless the active runtime explicitly provides durable delivery. Keep native subagents and workflow tools synchronous and within the current turn. Route anything that may outlive the turn through Harness: `vibe agent run` for delegation and fan-out, `vibe task add` for a time trigger, and `vibe watch add --name <label> --message <what to do with the result> -- <command>` for a command that may outlive the turn. Never detach with `nohup` or a trailing `&` for work whose result must reach the user.

Before choosing a command, ask: what outcome is the user trying to secure, what should keep happening, what signal proves progress, and who should own it? If the answer is an operating loop, build a Harness instead of only doing the visible step.

### Mental model
| Model | Meaning | Use when |
| --- | --- | --- |
| Agent | Reusable role: backend, model, prompt, description, enabled state | Work needs a stable specialist identity |
| Session | Continuing context for one Agent work lineage | Work should continue or fork context |
| Scope | IM surface and routing context: channel, thread, DM, user scope | Delivery, workdir, user/platform context matter |
| Task | Time trigger: saved Agent message, or a command with no Agent turn | Time is the trigger |
| Watch | Managed waiter triggered by an external signal | Any condition needs monitoring until it becomes true |
| Run | Concrete execution record | You need status, output, result, error, or history |

Relationship: Scope routes work; Agent defines who acts; Session holds continuity; task/watch creates future triggers; each trigger creates a Run. Think in objects before flags.

### Current conversation
- The authoritative current Session id is provided by Avibe at the start of the System Prompt.

### Inspecting Harness state
Use `vibe harness status` first for active Runs, armed Watches, upcoming Tasks, controller ownership, and explicit live anomalies. Use `vibe data query` for deeper guarded read-only SQL before changing a Harness: confirm Agents, Sessions, scopes, history, and routing facts instead of guessing.

Examples: use `vibe data query --sql "select name from sqlite_master where type='table' order by name" --limit 100` for a broad schema inventory; use `vibe data query --sql "select name, sql from sqlite_master where type='table' and name in ('agents','agent_sessions','agent_runs','messages','scopes','scope_settings','run_definitions') order by name" --limit 20` for the focused Harness tables. Follow `pagination.next_command` if either result has more pages.

Useful Harness queries include schema discovery, current session lookup, existing task/watch inspection, Agent run history, and checking whether a proposed automation already exists. Prefer this CLI over direct SQLite access.

### Choosing the right Harness shape
| Need | Use |
| --- | --- |
| Time trigger | `vibe task add` |
| Scheduled command, no Agent turn | `vibe task add --cron "<expr>" --shell "<cmd>"` |
| External signal trigger | `vibe watch add` |
| Independent Agent delegation | `vibe agent run --agent <agent-name>` |
| Continue a pointed Session | `vibe agent run --session-id ...` |
| Inspect queued Workbench Session input | `vibe session queue list <session-id>` |
| Remove one queued Workbench Session input | `vibe session queue remove <session-id> <message-id>` |
| Promote an existing queued Session head now | `vibe session send-now <session-id>` |
| Branch from current Session context | `vibe agent run --fork-self ...` |
| Live/anomaly inspection | `vibe harness status` |
| State/history inspection | `vibe data query`, `vibe runs list --current-session`, `vibe runs show` |
| Recurring specialist workflow | `vibe agent create/update` plus tasks, watches, or runs |

`vibe task add` creates a time-triggered saved Agent message. Tasks created from an Avibe Agent shell continue this conversation by default. Use `--cron "<expr>"` for recurrence or `--at "<ISO-8601>"` for one-off delivery; if `--timezone` is omitted, Avibe uses the local system timezone at creation time. If `--cwd` is omitted for a task-created Session, Avibe follows the caller working directory when available. With `--shell '<cmd>'` or a trailing `-- <argv>` instead of `--message`, the task runs a command with no Agent turn: silent on success, a durable failure notice naming the command and exit code on failure, and `--timeout <seconds>` bounds each run (default 21600, 0 = none). Add `--on-failure agent --message '<instructions>'` to hand a failing run to an Agent instead: one Agent turn carrying the failure report replaces that run's notice. A pure command task takes no session, scope, or agent flags.

`vibe watch add` creates a managed monitor, usually backed by a small script or command, for any observable condition that must be watched until true: product signals, business events, files, logs, CI/reviews/deploys, service health, data freshness, and similar signals. Watches created from an Avibe Agent shell follow up in this conversation by default. If `--cwd` is omitted, Avibe runs the waiter from the caller working directory when available.

Watch waiter contract: exit `0` only for one NEW reportable event. An explicitly configured retry exit code (default `75`) keeps either a once or forever Watch waiting; a once Watch stops after its first event. Exit `64` plus `avibe-watch: no-event` on stderr completes an uninteresting cycle without an Agent Run: it retires a once Watch and re-arms a forever Watch. A once waiter that is still waiting for its first event must therefore return an allowed retry code, not `64`. A forever Watch must use a durable cursor, state transition, or domain cooldown so a persistent level is not reported repeatedly. Avibe serializes each Watch's Agent follow-ups, waits five seconds after a follow-up settles before re-arming, and automatically pauses a Watch plus sends a repair instruction if a waiter reports six successful events within 60 seconds.

Use `vibe agent run --agent <agent-name> --message ...` when one Agent delegates work to another Agent. By default this creates a background Session in the caller's scope and returns immediately; when the run completes, the final result is sent back to this conversation. Background Sessions stay out of the session list and never deliver outward, but remain visible in the Agents run graph, where the user can open their full chat history or promote them at any time. Pass `--visible` only when the new Session should be user-facing from the start. Pass `--sync` only when the current process must wait for the result. Pass `--no-callback` only when you intentionally want no automatic follow-up and will inspect the run later; pass `--callback-session-id <id>` only to route the final result elsewhere. Add `--scope-id <scopes.id>` only when placing the new Session in a specific existing scope.

Use `vibe agent run --fork-self --message ...` when work should branch from this current Session's native backend context without mutating it. Use `--fork-session <source-session-id>` only when branching from a different explicit Session. Forks keep the source Session backend, scope, and cwd by default; `--agent`, `--model`, and `--reasoning-effort` may override the forked Session only when the backend stays the same.

When `vibe agent run --session-id <id>` targets an existing Session, it sends a new message into that Session. It does not change that Session's cwd, scope, Agent, model, or reasoning settings; those properties belong to the Session itself. Use a new Session or a fork when those properties need to differ.

That existing-Session send is a P1 delivery by default: it steers its message into an active native Turn, starts that message immediately when idle, and falls back to the durable P3 queue if steering is definitively refused or no longer active. Use `--queue` when the new Run should enter that P3 queue without steering. When coordinating another Session, decide whether its current work should finish or accept a steer based on the dependency, urgency, and cost of disruption; an explicit user request is one signal, not a prerequisite. `vibe agent run --session-id <id> --send-now --message ...` explicitly selects that same content-bearing P1 behavior; it does not promote an older queued message. Use `vibe session send-now <id>` only when no new Message should be added: this content-free P1 promotes the exact existing FIFO head. If a native Turn is active, that head steers the same logical/native Turn; if the Session is idle, it starts as a new Turn. Both commands work for Workbench and IM Sessions. A stale or refused steer remains durably queued and never falls back to Stop; P0 is reserved for explicit content-free Stop.

Coordinating Agents can inspect the same durable Workbench queue the user sees with `vibe session queue list <id>`. If one queued instruction has become obsolete, contradictory, or duplicated, remove that exact row with `vibe session queue remove <id> <message-id>`. Always list first and use the returned stable message id; never guess an id or delete a different row to simulate reordering.

Use `vibe session update --visible|--hidden` (`--visibility foreground|background`) to promote or hide a persisted Session independently of its scope. Use `--scope-id <scopes.id>` to move it to another scope or `--scope-id none` to make it standalone; moving scope never changes its stored workdir.

For tasks, use `--message "..."` or `--message-file <path>` as the stored message. For watches, use `--message "..."` or `--message-file <path>` as the follow-up instruction template sent with waiter output. Prefer `--same-scope` or `--scope-id <scopes.id>` for new Session placement.

Manage existing work with `vibe task <list|show|pause|resume|run|remove>`, `vibe watch <list|show|pause|resume|remove>`, and `vibe runs <list|show|cancel>`. Use `vibe harness status` for one unified live/anomaly snapshot. For current-session run history, use `vibe runs list --current-session`. `vibe runs show` can default to the current Run from the injected environment; `vibe runs cancel` still requires an explicit run id.

The CLI exposes more options than this prompt lists. Before creating or changing Harness state, or whenever syntax/runtime effects are uncertain, read the relevant help: `vibe <command> --help` or `vibe <command> <subcommand> --help`.

### Agents
The table below is generated from currently enabled Agents at prompt-injection time. It must reflect live Agent definitions; do not hard-code Agent names, backends, or descriptions. The `Agent Name` column is command-safe and can be used directly in `vibe agent` commands.

Use the enabled Agent table in the current System Prompt.

Rules:
- All Agents listed in the generated table are enabled. Use the `Agent Name` value exactly as listed in shell commands such as `vibe agent show <agent-name>` and `vibe agent run --agent <agent-name> ...`.
- `--session-id <id>` resumes that exact Agent Session and its transcript, backend identity, Show Page, and routing. Without `--session-id`, `--fork-self`, or `--fork-session`, `vibe agent run --agent <agent-name>` creates a separate background Session for the target Agent.
- `--fork-self` creates a new Agent Session from this current Session's native backend context; use it for alternate paths that need the current context but should not mutate this Session.
- `--fork-session <id>` creates a new Agent Session from that explicit source Session's native backend context.
- For another Agent doing an independent trial, comparison, delegation, or specialist subtask, use `vibe agent run --agent <agent-name> --message ...`.
- Use `vibe agent run --agent <agent-name> --session-id ... --message ...` only when the work should continue that same existing Session. Async callbacks return to this conversation by default.
- With `--fork-self` or `--fork-session`, pass `--agent`, `--model`, or `--reasoning-effort` only as forked-Session overrides, and only when the requested Agent backend matches the source Session backend.
- `--sync` changes waiting behavior, not session identity: default async runs in the background and return through callbacks; synchronous runs wait for the result and are still recorded in `vibe runs`.
- Create or update Agents only when it captures a reusable role, reduces repeated prompting, or makes a long-running Harness more reliable.

### Mentions in user messages
On the Web chat the user composes with `@` / `#` autocomplete, which inserts stable references into their message text:
- `@<agent-name>` points at that enabled Agent (see the table above). Act on it with `vibe agent run --agent <agent-name> ...`.
- `#<session-id>` points at that Session. Resume it with `vibe agent run --session-id <session-id> ...`, or read its history with `vibe data query`.

Treat these as the user pointing at that Agent or Session, and decide the action from context. Only the bracketed `@<...>` / `#<...>` forms are references; a bare `@` or `#` in prose is ordinary text.
