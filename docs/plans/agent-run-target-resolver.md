# Agent Run Target Resolver

## Status

Draft. This plan captures the root fix for Workbench sessions starting agents
from the wrong directory, and generalizes it into a shared execution-target
model for every platform.

## Problem

Vibe Remote stores working directories in a shared table, but runtime resolution
is not shared.

Current behavior:

- IM channel and DM scopes store their cwd in `scope_settings.workdir`, then
  load it through the runtime `SettingsManager` as `custom_cwd`.
- Workbench projects also store their folder in `scope_settings.workdir`.
- Workbench sessions snapshot the project folder into `agent_sessions.workdir`.
- Workbench dispatch injects the session row into
  `platform_specific["agent_session_target"]`, including its `workdir`.
- `Controller.get_cwd(context)` ignores `agent_session_target.workdir` and only
  asks the IM settings facade for `custom_cwd`.

That leaves Avibe/Web UI contexts with no matching IM custom cwd. When there is
no global default cwd, `get_cwd()` falls back to the Vibe Remote process cwd.
If the service was launched from `/tmp/test`, a Workbench session can start its
agent from `/tmp/test` even though the selected project is
`/workspace/avibe-project`.

Some backends then bind their native session id back into the reserved
`agent_sessions` row with `request.working_path`, which can persist the wrong
cwd onto the Workbench session.

## Goal

Create one shared runtime resolver for "where and how this agent turn should
run".

After this plan lands:

- Every agent turn uses the same resolver for cwd, scope, session anchor,
  session row, agent, backend, model, and reasoning effort.
- Adding a new platform does not require remembering platform-specific cwd
  fallbacks in agent handlers.
- `scope_settings.workdir` remains the source of default scope cwd.
- `agent_sessions.workdir` becomes the source of truth for an existing session's
  actual bound cwd.
- `Controller.get_cwd(context)` becomes a compatibility wrapper over the shared
  resolver instead of a separate policy engine.

## Non-Goals

- Do not change the database schema unless implementation finds a missing
  constraint. The existing `scopes`, `scope_settings`, and `agent_sessions`
  tables are enough for the target model.
- Do not merge Workbench and IM UX concepts. Workbench projects and IM channels
  can remain different product surfaces; only execution target resolution is
  unified.
- Do not silently move existing native sessions to a new cwd. Existing sessions
  should resume from their persisted `agent_sessions.workdir`.

## Target Model

### Scope default

`scope_settings.workdir` means:

> The default working directory for new sessions created in this scope.

Examples:

- `slack::channel::C09KX3GN118`
- `slack::user::U0E0FM3QT`
- `lark::channel::oc_...`
- `avibe::project::proj_272e944ca452`

This is not the resume source of truth once a concrete `agent_sessions` row
exists.

### Session binding

`agent_sessions.workdir` means:

> The working directory snapshot bound to this concrete agent session.

That value must be stable for resume. If a project or channel's default cwd
changes later, existing sessions keep their original cwd unless an explicit
session-level move feature is designed.

### Execution target

Introduce a value object, for example:

```python
@dataclass(frozen=True)
class AgentRunTarget:
    platform: str
    scope_id: str | None
    scope_type: str | None
    settings_key: str
    session_key: str
    agent_session_id: str | None
    session_anchor: str
    workdir: str
    agent_name: str | None
    agent_backend: str | None
    model: str | None
    reasoning_effort: str | None
    source: str
```

The exact fields can be adjusted during implementation, but the resolver must
return one object that all agent-start paths consume.

## Resolution Rules

### 1. Existing reserved session wins

If the context carries an agent-session target:

- Read by `agent_session_target.id` or `platform_specific["agent_session_id"]`.
- Use `agent_sessions.scope_id`, `session_anchor`, `workdir`, and pinned agent
  fields from that row.
- If the target payload is present but incomplete, reload the row from storage
  instead of falling back to process cwd.

This is the Workbench path, but the rule is deliberately generic so scheduled
tasks and watches can also resume by session id.

### 2. Existing IM thread session wins

If this is an IM turn and a row already exists for `(scope_id, session_anchor)`:

- Use the existing `agent_sessions` row.
- Use `agent_sessions.workdir` for cwd.
- Use the session's pinned agent/backend values.

This keeps existing IM threads stable when a channel default changes.

### 3. New session seeds from scope

If no existing session row is bound:

- Resolve the scope from context:
  - IM channel: platform + channel id.
  - IM DM: platform + user id.
  - Workbench project: project scope id.
  - Scheduled/watch: explicit target session if present, otherwise explicit
    target scope.
- Read default cwd and routing from `scope_settings`.
- Create or reserve the session row with `agent_sessions.workdir =
  scope_settings.workdir`.

### 4. Fallbacks are explicit and noisy

Only after the resolver has proved there is no session cwd and no scope cwd:

1. Use configured global default cwd, if any.
2. Use process cwd as last resort.
3. Log a warning that includes platform, settings key, session key, and whether
   an agent session id was present.

The process cwd fallback should be a diagnosable escape hatch, not normal
Workbench behavior.

## Implementation Plan

### Phase 1: Resolver service

Add a shared service under `core/services/`, for example
`core/services/agent_run_target.py`.

Responsibilities:

- Normalize platform, settings key, session key, and session anchor.
- Resolve a concrete `agent_sessions` row when possible.
- Resolve the current scope row when no session exists.
- Return an `AgentRunTarget`.
- Validate and normalize `workdir`.
- Avoid creating hidden sessions unless the caller explicitly asks for
  reservation.

The first version can be read-only plus an explicit `ensure_session=True` option
for paths that need to reserve a row before prompt injection.

### Phase 2: Wire cwd callers

Change these callers to use the resolver:

- `Controller.get_cwd(context)`
- `SessionHandler.get_session_info(context)`
- `MessageHandler` when constructing `AgentRequest`
- Claude, Codex, and OpenCode preflight paths that currently re-read cwd
- command paths such as `/cwd` and `/set_cwd`

`get_cwd()` should remain as a public compatibility method, but its body should
become resolver-backed.

### Phase 3: Wire routing callers

Move agent/backend/model/effort resolution onto the same target object.

Replace split reads from:

- `settings_manager.get_channel_routing(settings_key)`
- `agent_session_target`
- Workbench header overrides
- global default agent

with a single resolver result consumed by `MessageHandler` and backend agents.

This prevents the cwd fix from leaving routing with the same split-brain
problem.

### Phase 4: Bind without cwd pollution

Update native-session binding helpers so reserved-session bind does not blindly
overwrite a valid `agent_sessions.workdir` with `request.working_path` unless
that request path came from the resolver.

Acceptable behavior:

- If the row has no workdir, set it from the resolver target.
- If the row has the same workdir, keep it.
- If the row has a different workdir, do not overwrite silently. Log and fail
  loud for resume-sensitive paths, or require an explicit migration operation.

### Phase 5: Cleanup compatibility names

Keep API compatibility for `custom_cwd`, but stop treating it as a separate
runtime concept.

Suggested direction:

- Public IM settings can still expose `custom_cwd`.
- Internal services should use `workdir`.
- `SettingsManager.get_custom_cwd()` can become a thin compatibility wrapper
  over the scope resolver or scope settings reader.

## Data Repair

After the code-level fix, repair already polluted Workbench sessions separately.

Safe repair rule:

- Only target `agent_sessions` rows whose `scope_id` points to an
  `avibe::project::*` scope.
- Only repair rows whose `workdir` equals a known fallback such as `/tmp/test`.
- Replace with the owning project scope's `scope_settings.workdir`.
- Do not modify rows where `native_session_id` is non-empty unless we confirm
  the backend native session can still resume under the project cwd.

The last condition matters: changing `agent_sessions.workdir` for an already
native-bound session may make Claude/Codex resume look in a different project
state location.

## Test Plan

### Unit tests

- Workbench dispatch with `agent_session_target.workdir` resolves that workdir.
- Workbench dispatch with incomplete target reloads `agent_sessions` by id.
- IM channel turn resolves channel `scope_settings.workdir`.
- IM DM turn resolves user `scope_settings.workdir`.
- Existing IM thread resolves `agent_sessions.workdir` even after the channel
  scope cwd changes.
- Missing scope cwd falls back to global cwd and logs a warning.
- Process cwd fallback is covered but treated as last resort.

### Integration tests

- Create an Avibe project with cwd `/repo`.
- Create a Workbench session in that project.
- Start a Codex turn while the Vibe Remote process cwd is `/tmp/test`.
- Assert the Codex request/transport cwd is `/repo`.
- Assert `agent_sessions.workdir` remains `/repo` after native bind.

Repeat the same shape for Claude and OpenCode.

### Regression tests

- Existing Slack/Lark channel cwd behavior remains unchanged.
- `/set_cwd` updates the correct channel/user scope row.
- Workbench project folder update affects new sessions, not existing sessions.
- Scheduled task/watch follow-up into an existing session uses that session's
  `agent_sessions.workdir`.

## Risks

- Cwd, routing, and session pinning are central to all platforms. This needs
  broad tests before merging.
- Existing code uses `settings_key` for both settings lookup and session
  grouping in several places. The resolver must keep `settings_key` and
  `session_key` explicit to avoid reintroducing cross-platform collisions.
- Backends differ in when native session ids become available. The resolver
  should not assume native id exists before prompt injection.
- Data repair can strand native sessions if it changes cwd for already-bound
  sessions. Repair must be conservative.

## Rollout Plan

1. Land resolver and tests with `get_cwd()` still exposing the old method name.
2. Move cwd consumers onto resolver-backed `get_cwd()`.
3. Move routing consumers onto the target object.
4. Add a read-only diagnostic command or debug log to print the resolved target
   for a turn.
5. Repair polluted Workbench rows after validating native resume behavior.

## Open Questions

- Should Workbench allow a session-level cwd override distinct from project cwd,
  or should cwd always be immutable after session creation?
- Should `/set_cwd` on IM scopes affect only new sessions, or should it offer an
  explicit "move current thread" operation?
- Should `agent_sessions.workdir` be nullable long-term, or should new session
  creation require a resolved cwd?
- Should process cwd fallback eventually become an error for Workbench contexts?
