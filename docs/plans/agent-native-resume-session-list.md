# Agent-Native Resume Session List

## Background

The current `/resume` flow reads Vibe Remote's own persisted session bindings from `SessionsStore`.
That is sufficient for "resume the session previously bound to this thread", but it does not solve the
actual user problem:

- users want to browse the real session history from the agent backend itself
- the list should reflect the current effective working directory
- sessions created outside the current IM thread should still be recoverable
- the list should be visually obvious across multiple agent backends

This design replaces the current resume picker source with an agent-native session catalog.

## Goal

When the user opens Resume Session, Vibe Remote should:

1. Resolve the current effective working directory via the existing controller/session handler logic.
2. Read the real session history for OpenCode, Claude Code, and Codex for that directory.
3. Merge all sessions into one list.
4. Sort the merged list by time descending, newest first.
5. Show each row with:
   - session time
   - agent prefix: `oc-`, `cc-`, `cx-`
   - the tail of the agent's last message, truncated to about 10 characters
6. On selection, bind the chosen native session ID to the current thread using the existing resume submission flow.

## Non-Goals

- Do not change the actual resume behavior per backend.
- Do not remove manual session ID input.
- Do not change automatic same-thread resume.
- Do not introduce new dependencies.

## Product Rules

### Source of truth

The resume list must no longer come from `SessionsFacade.list_all_agent_sessions(...)`.

That mapping remains useful only as the runtime binding target after the user explicitly chooses a session.

### Working directory

The list must use the real effective working directory for the current context:

- source: `SessionHandler.get_working_path(context)`
- not the process cwd
- not the repository root unless that is the actual effective cwd

### Unified row format

Each displayed item should represent one native backend session and expose:

- `agent`: `opencode` | `claude` | `codex`
- `agent_prefix`: `oc` | `cc` | `cx`
- `native_session_id`: backend-native resume identifier
- `sort_ts`: primary timestamp used for global sorting
- `display_time`: localized short timestamp
- `last_agent_tail`: tail of the backend's latest assistant message
- `display_label`: platform-formatted label

Important: `display_id` is only for UI. Resume submission must still pass the real native identifier:

- OpenCode: session ID such as `ses_xxx`
- Claude Code: session ID UUID
- Codex: thread ID

## Architecture

### New module

Add a new backend-facing session catalog layer under `modules/agents/native_sessions/`:

- `types.py`
- `base.py`
- `opencode.py`
- `claude.py`
- `codex.py`
- `service.py`

This keeps backend-specific session discovery in `modules/agents/` and keeps platform-agnostic orchestration in handlers.

### Core types

```python
@dataclass
class NativeResumeSession:
    agent: Literal["opencode", "claude", "codex"]
    agent_prefix: Literal["oc", "cc", "cx"]
    native_session_id: str
    working_path: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    sort_ts: float
    last_agent_message: str
    last_agent_tail: str
    locator: dict[str, Any]
```

```python
class NativeSessionProvider(Protocol):
    agent_name: str

    def list_metadata(self, working_path: str) -> list[NativeResumeSession]:
        ...

    def hydrate_preview(self, item: NativeResumeSession) -> NativeResumeSession:
        ...
```

```python
class AgentNativeSessionService:
    def list_recent_sessions(
        self,
        working_path: str,
        limit: int,
    ) -> list[NativeResumeSession]:
        ...
```

### Two-phase loading

To avoid slow picker opens:

1. Each provider first returns lightweight metadata only.
2. The service merges all metadata and sorts by `sort_ts desc`.
3. Only the top `limit` items are hydrated to compute `last_agent_tail`.

This is especially important for Claude Code and Codex, where preview extraction requires reading session files.

## Backend Providers

### OpenCode provider

#### Source

- database: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`
- read via Python `sqlite3` in read-only mode

#### Metadata query

Read from `session`:

- filter: `directory = working_path`
- fields:
  - `id`
  - `title`
  - `time_created`
  - `time_updated`

Use `time_updated` as the primary sort timestamp.

#### Preview query

Read from `part`:

- filter by `session_id`
- order by `time_created desc`
- find the latest assistant text part

The `part.data` payload already contains structured text parts, so OpenCode does not need `opencode export` for picker rendering.

#### Native resume identifier

- `native_session_id = session.id`

### Claude Code provider

#### Source

- project index: `~/.claude/projects/<encoded-working-path>/sessions-index.json`
- session files: `~/.claude/projects/<encoded-working-path>/<session_id>.jsonl`

Directory encoding rule:

- absolute path with `/` replaced by `-`
- example:
  - `/Users/alice/avibe`
  - `-Users-alice-avibe`

#### Metadata source

Primary source: `sessions-index.json`

Useful fields:

- `sessionId`
- `created`
- `modified`
- `projectPath`
- `firstPrompt`
- `fullPath`

Fallback if the index is missing or stale:

- scan `*.jsonl` files in the encoded project directory
- derive timestamps from file metadata and/or JSONL content

#### Preview extraction

Read the referenced `.jsonl` and scan for the latest assistant message with textual content.

Preferred order:

1. latest assistant text
2. fallback to session name if present in future index schema
3. fallback to `firstPrompt`

#### Native resume identifier

- `native_session_id = sessionId`

### Codex provider

#### Source

- metadata DB: `~/.codex/state_5.sqlite`
- message log file: `threads.rollout_path`

#### Metadata query

Read from `threads`:

- filter: `cwd = working_path`
- fields:
  - `id`
  - `created_at`
  - `updated_at`
  - `title`
  - `first_user_message`
  - `rollout_path`

Use `updated_at` as the primary sort timestamp.

#### Preview extraction

Parse `rollout_path` JSONL and scan backwards for the latest assistant-visible text.

Acceptable sources include:

- `response_item` assistant messages
- `event_msg` assistant commentary if that is the last visible assistant content

For picker preview, use the latest assistant text that a user would recognize as the session's final reply.

#### Native resume identifier

- `native_session_id = thread.id`

This matches the current Codex resume path, which calls `thread/resume` with the stored thread ID.

## Preview Text Rules

The picker preview should show the tail of the last assistant message, not the title.

### Normalization

For all providers:

1. keep only assistant-authored text
2. normalize newlines to spaces
3. trim whitespace
4. remove trailing quick-reply button block after `\n---\n`
5. drop empty output

### Tail extraction

Use the last non-empty line after normalization.

If the line is too long:

- keep the last ~10 characters
- prepend `...`

Example:

- full: `请帮我调研清楚这三种 agent 是否具备获取session list及详情的这样的能力。`
- tail: `...详情的这样的能力。`

### Empty fallback

If no assistant text exists yet:

- OpenCode: fallback to session title
- Claude Code: fallback to `firstPrompt`
- Codex: fallback to `title` or `first_user_message`

## Sorting and Merge Rules

### Global ordering

Merge all provider items into one flat list and sort by:

1. `sort_ts desc`
2. `agent_prefix asc`
3. `native_session_id asc`

This guarantees deterministic output.

### Time source

- prefer `updated_at`
- fallback to `created_at`

### Display time

Use a compact localized format suitable for IM pickers, for example:

- same year: `03-25 22:54`
- older year: `2025-11-10`

The exact formatter can stay shared and platform-agnostic.

## UI Design

### Required visual signal

Users must be able to tell backend type immediately.

Use explicit prefixes:

- OpenCode: `oc-`
- Claude Code: `cc-`
- Codex: `cx-`

### Canonical row content

Canonical display payload:

- prefix
- time
- last tail

Example logical row:

- `oc- 03-25 22:54  ...提交到master`

### Platform-specific formatting

#### Slack

Use a single flat `static_select` list, not grouped-by-agent option groups.

- option text: `oc- 03-25 22:54`
- option description: `...提交到master`
- option value: `opencode|ses_xxx`

This preserves global time ordering while still showing the preview text.

#### Discord

Use one flat select:

- label: `oc- 03-25 22:54`
- description: `...提交到master`
- value: `opencode|ses_xxx`

Because Discord caps select options at 25:

- show the newest 25 merged sessions
- keep manual session ID input as the escape hatch

#### Feishu

Feishu `select_static` is more constrained, so encode everything into one visible line:

- text: `oc- 03-25 22:54 · ...提交到master`
- value: `opencode|ses_xxx`

Show the newest 100 merged sessions.

## Handler Changes

### `CommandHandlers.handle_resume`

Current behavior:

- reads `self.sessions.list_all_agent_sessions(session_key)`

New behavior:

1. resolve `working_path = self.controller.get_cwd(context)`
2. call `agent_native_session_service.list_recent_sessions(working_path, platform_limit)`
3. pass the flat merged list into the platform modal/card renderer
4. if empty, still open the modal when manual paste is supported

### IM modal signatures

Change the modal/card methods from:

```python
open_resume_session_modal(..., sessions_by_agent: Dict[str, Dict[str, str]], ...)
```

to:

```python
open_resume_session_modal(..., sessions: list[NativeResumeSession], ...)
```

All three IM implementations should render from the same flat list.

### Resume submission

No behavioral rewrite is required.

Keep the existing submission contract:

- parse selected value as `agent|native_session_id`
- call `handle_resume_session_submission(...)`
- persist the chosen backend-native identifier via the existing session binding flow

This means:

- OpenCode still stores the chosen `ses_xxx`
- Claude still stores the chosen session UUID
- Codex still stores the chosen thread ID

## Copy Changes

The old copy says "stored sessions", which will become inaccurate.

Update copy to reflect the new source:

- "Recent sessions in the current working directory"
- "No recent agent sessions found for this working directory"
- manual paste remains available

## File Touchpoints

Expected implementation touchpoints:

- `core/handlers/command_handlers.py`
- `modules/im/slack.py`
- `modules/im/discord.py`
- `modules/im/feishu.py`
- `core/handlers/session_handler.py`
- `modules/agents/native_sessions/*`
- `vibe/i18n/en.json`
- `vibe/i18n/zh.json`

The existing `SessionsFacade` remains in place for thread-to-session bindings after selection.

## Edge Cases

### Missing backend data

If a backend is not installed, not enabled, or its local state files are missing:

- skip that backend
- do not fail the whole picker
- log at `info` or `warning`

### Corrupt session entries

If one session file or row fails to parse:

- skip that item
- continue rendering the rest

### Duplicate-looking IDs

Because the display prefix is explicit and the value carries `agent|native_id`, collisions are harmless.

### Sessions with no assistant reply yet

Still list them if they have valid metadata.

Their preview should fallback as described above.

## Testing Plan

### Unit tests

Add provider-level tests for:

- OpenCode metadata filtering by exact `working_path`
- Claude project path encoding and index parsing
- Codex thread filtering by exact `cwd`
- tail extraction and truncation rules
- merged global sorting

### Handler tests

Update `/resume` tests to verify:

- list source is agent-native, not session map
- merged order is newest first across backends
- selected value still resumes correctly

### Manual checks

Verify in Slack, Discord, and Feishu:

1. sessions from all three backends appear in one merged list
2. prefixes are visible and correct
3. newest items are first
4. preview shows the tail of the last agent message
5. selecting an item binds and resumes correctly

## Rollout Strategy

Implement in two steps:

1. introduce the new native session service and swap the picker source
2. keep the existing manual session ID path and binding logic untouched

This keeps the change focused and lowers regression risk.
