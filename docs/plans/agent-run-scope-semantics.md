# Agent Run Scope Semantics

## Problem

The Harness CLI used to expose backend and transport details directly to
agents. In particular, `vibe agent run`, `vibe task`, and `vibe watch` relied on
explicit session ids, delivery keys, and post targets even when Avibe already
knows the calling Agent Session through `AVIBE_SESSION_ID`.

That made agents carry routing details in prompts instead of using the runtime
context as a first-class API.

## Principles

- `AVIBE_SESSION_ID` identifies the caller session.
- Callback routing is a run-level completion policy, not a session-level
  default. Synchronous runs should still record the callback route so a future
  long-running sync turn can detach into async without losing its return path.
- Scope placement is a session-level decision. It decides where a newly-created
  session lives: a private/background scope, the caller/source scope, or a
  specific `scopes.id`.
- Message delivery overrides are not scope placement. `post-to` remains a reply
  delivery override; it does not move a session.
- `deliver-key` is legacy transport syntax and should leave the agent-facing
  CLI surface. New commands should use `--scope-id` or `--same-scope`.

## `vibe agent run` Defaults

When an agent runs another agent with no explicit target:

```bash
vibe agent run --agent claude --message "Review this patch"
```

Avibe treats it as:

- create a new private/background session;
- use the caller shell cwd as the new session cwd;
- record callback route to the caller session when `AVIBE_SESSION_ID` exists;
- keep the run synchronous unless `--async` is explicitly passed.

The async-by-default proposal is intentionally left as a follow-up. It changes
the waiting contract of every existing caller and should land separately with a
focused migration.

## New Target And Scope Parameters

| Parameter | Meaning |
| --- | --- |
| `--fork-self` | Fork the current caller session without passing its id. Requires `AVIBE_SESSION_ID`. |
| `--fork-session <session-id>` | Fork the specified source session. |
| `--same-scope` | Place a newly-created session in the caller/source session's scope. |
| `--scope-id <scopes.id>` | Place a newly-created session in the exact scope row. |
| no scope parameter on create | Create a private/background session. |

Forks inherit the source session's scope and cwd by default. `--same-scope` is
therefore redundant but acceptable for fork commands. `--scope-id` may be used
to fork into a different scope.

## CWD Rules

| Operation | Default cwd |
| --- | --- |
| create private/background session | caller shell cwd |
| create with `--same-scope` | caller shell cwd |
| create with `--scope-id` | caller shell cwd |
| fork self/session | source session cwd |
| continue existing session | existing session cwd |
| explicit `--cwd` | wins for blank create only |

## Callback Route Rules

All `vibe agent run` invocations resolve a callback route:

- explicit `--callback-session-id` wins;
- `--no-callback` records an intentional no-callback policy;
- otherwise, `AVIBE_SESSION_ID` becomes the callback route when available.

For async runs, the callback route is used when the run completes. For sync
runs, the route is only recorded for future detach-to-async behavior.

## Other CLI Defaults

- `vibe runs list` remains global by default.
- `vibe runs list --current-session` filters to `AVIBE_SESSION_ID`.
- `vibe runs show` may default to `AVIBE_RUN_ID`.
- `vibe runs cancel` stays explicit; no `--current-run` shortcut in this batch.
- Single-object session commands may default object id from `AVIBE_SESSION_ID`.
- `vibe task add` / `vibe watch add` default their target session to
  `AVIBE_SESSION_ID` when they continue an existing session.
- When a task or watch creates sessions, agents should use `--same-scope` or
  `--scope-id <scopes.id>`. The selected placement scope is stored in definition
  metadata as `session_scope_id`, so future `create-session-per-run` triggers
  can create the session in the right Workbench project or IM scope.
- `vibe task add` and `vibe watch add` snapshot the caller shell cwd when
  `--cwd` is omitted. That cwd is reused when the definition later creates
  sessions.

## Migration

This batch removes `deliver-key` from recommended agent-facing help, prompt
injection, and examples. Internal persistence can keep legacy field names until
a later database cleanup. The CLI may continue accepting hidden legacy
`--deliver-key` for compatibility, but new code paths write placement through
`scope_id` semantics.
