# Agent Run Scope Semantics

## Problem

The Harness CLI used to expose backend and transport details directly to
agents. In particular, `vibe agent run`, `vibe task`, and `vibe watch` relied on
explicit session ids and legacy delivery-target syntax even when Avibe already
knows the calling Agent Session through `AVIBE_SESSION_ID`.

That made agents carry routing details in prompts instead of using the runtime
context as a first-class API.

## Principles

- `AVIBE_SESSION_ID` identifies the caller session.
- Callback routing is a run-level completion policy, not a session-level
  default. Synchronous runs should still record the callback route so a future
  long-running sync turn can detach into async without losing its return path.
- Scope placement is a session-level decision. It decides whether a newly-created
  session lives under the caller/source scope, a specific `scopes.id`, or as a
  standalone session (`scope_id=NULL`). Visibility is independent of placement.
- Message delivery overrides are not scope placement. Legacy transport
  overrides should leave the agent-facing CLI surface.
- New commands should use `--scope-id` or `--same-scope`.

## `vibe agent run` Defaults

When an agent runs another agent with no explicit target:

```bash
vibe agent run --agent claude --message "Review this patch"
```

Avibe treats it as (superseded by
[`agents-run-graph-and-session-visibility.md`](agents-run-graph-and-session-visibility.md#part-c--scope--visibility-decoupling--standalone-sessions)):

- create a new background session in the caller session's scope;
- use the caller shell cwd as the new session cwd;
- record callback route to the caller session when `AVIBE_SESSION_ID` exists;
- queue the run and return immediately by default.

Use `--sync` only when the CLI process must wait for the run result. The legacy
`--async` flag remains accepted for older scripts, but it is no longer required.

## New Target And Scope Parameters

| Parameter | Meaning |
| --- | --- |
| `--fork-self` | Fork the current caller session without passing its id. Requires `AVIBE_SESSION_ID`. |
| `--fork-session <session-id>` | Fork the specified source session. |
| `--same-scope` | Place a newly-created session in the caller/source session's scope. |
| `--scope-id <scopes.id>` | Place a newly-created session in the exact scope row. |
| no scope parameter on create, with caller | Use the caller scope and background visibility. |
| no scope parameter on create, without caller | Create a standalone background session. |

Forks inherit the source session's scope and cwd by default. `--same-scope` is
therefore redundant but acceptable for fork commands. `--scope-id` may be used
to fork into a different scope.

## CWD Rules

| Operation | Default cwd |
| --- | --- |
| create with implicit caller scope | caller shell cwd |
| create standalone without caller | session Show workspace |
| create with `--same-scope` | selected scope workdir |
| create with `--scope-id` | selected scope workdir |
| fork self/session | source session cwd |
| continue existing session | existing session cwd |
| explicit `--cwd` | wins for blank create only |

## Callback Route Rules

All `vibe agent run` invocations resolve a callback route:

- explicit `--callback-session-id` wins;
- `--no-callback` records an intentional no-callback policy;
- otherwise, `AVIBE_SESSION_ID` becomes the callback route when available.

For default async runs, the callback route is used when the run completes. For
sync runs, the route is recorded for future detach-to-async behavior.

## Other CLI Defaults

- `vibe runs list` remains global by default.
- `vibe runs list --current-session` filters to `AVIBE_SESSION_ID`.
- `vibe runs show` may default to `AVIBE_RUN_ID`.
- `vibe runs cancel` stays explicit; no `--current-run` shortcut in this batch.
- Single-object session commands may default object id from `AVIBE_SESSION_ID`.
- `vibe task add` / `vibe watch add` default their target session to
  `AVIBE_SESSION_ID` when they continue an existing session.
- A task/watch `create-session-per-run` definition uses stored
  `session_scope_id` when present; without one it creates a standalone
  background session. See the newer Part C specification linked above.
- `--cwd` remains an explicit per-definition override. Without it, scoped
  create-per-run sessions snapshot the selected scope workdir at reservation;
  standalone create-per-run sessions use their own Show workspace.

## Migration

This batch removes legacy delivery-target syntax from recommended agent-facing
help, prompt injection, and examples. Internal persistence can keep legacy field
names until a later database cleanup. The CLI may continue accepting hidden
legacy placement input for compatibility, but new code paths write placement
through `scope_id` semantics.
