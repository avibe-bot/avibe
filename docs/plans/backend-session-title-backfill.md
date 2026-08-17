# Backend-native session title backfill

## Background

`agent_sessions.title` is currently a Vibe-owned display field. Workbench-created
sessions may have a title, but IM-created sessions usually leave it empty. Native
backend sessions also have their own naming behavior:

- **OpenCode** currently receives `title="vibe-remote:{base_session_id}"` when a
  new native session is created. This blocks OpenCode's own title generation,
  because OpenCode only auto-generates a semantic title while its title still
  matches the default `New session - <ISO timestamp>` format.
- **Codex** does not receive a title from Vibe Remote. Its local thread store has
  `title`, `first_user_message`, and `preview` fields.
- **Claude Code** does not receive a title from Vibe Remote through the current
  SDK path. The CLI has session naming / rename concepts, but the SDK options
  used here do not expose a first-class title/name field, and the stable local
  session index exposes `firstPrompt`, not a reliable generated `title`. Product
  direction: use the first user message's first 10 visible characters as the
  Claude fallback title.

The current OpenCode behavior is backwards for product quality: Vibe Remote
injects a technical title, then loses access to the backend's better title.

## Goal

Use backend-native titles as the source of truth for empty Vibe session titles:

1. Do not pass a Vibe-generated title/name to any backend when creating a native
   session.
2. Pull the backend's native title after a turn has enough context to name the
   conversation.
3. Backfill `agent_sessions.title` only while it is still empty and not
   user-owned.
4. Never overwrite a title the user has edited.

## Non-goals

- Do not add a Vibe-side LLM title generator in this change.
- Do not try to force Claude Code to accept a title via undocumented CLI flags
  or by mutating Claude's private storage.
- Do not rewrite historical session titles in a migration. This is a forward
  behavior change; optional historical backfill can be a separate maintenance
  command later.

## Backend Findings

### OpenCode

Confirmed behavior:

- `POST /session` with no title creates a default title like
  `New session - 2026-06-02T07:35:03.127Z`.
- OpenCode stores this in its sqlite `session.title` field and returns it in
  the create-session response.
- OpenCode source has an `ensureTitle` path that only runs when
  `Session.isDefaultTitle(session.title)` is true. After the first real user
  message, it asks the built-in `title` agent to generate a semantic title and
  calls `Session.setTitle`.

Implication:

- Vibe Remote must stop passing `vibe-remote:{base_session_id}`.
- The initial default title must be ignored for backfill.
- The semantic title should be read after the first turn completes, or on a
  short delayed retry if it is not available immediately.

### Codex

Current behavior:

- Vibe Remote starts Codex threads with `thread/start` params containing cwd,
  sandbox, approval policy, and developer instructions, but no title.
- Codex local state has `threads.title`, `threads.first_user_message`, and
  `threads.preview`.
- The existing `CodexNativeSessionProvider` already reads `title` from the
  native sqlite store for `/resume`.

Implication:

- Codex is already aligned with "do not pass title".
- Add a small title pull path that reads the current native thread by id and
  returns `threads.title` when it is non-empty and not a placeholder.

### Claude Code

Current behavior:

- The CLI supports `--name` and rename/display-name concepts.
- The current `ClaudeAgentOptions` used by Vibe Remote does not expose a
  first-class `name` or `title` field.
- Stable local discovery data currently exposes `sessions-index.json` fields
  such as `firstPrompt`, `sessionId`, timestamps, path, and message count.
  It does not expose a reliable generated `title`.
- `history.jsonl` has `display`, but that is a history-entry display string,
  not a stable session title contract.

Implication:

- Claude should not claim a backend-generated title.
- Claude should expose a low-confidence derived title from the first user
  message: the first 10 visible characters after trimming whitespace.
- Source should be `derived_first_prompt`, not `backend`.
- Normalize before slicing: trim, collapse whitespace/newlines to a single
  space, then take the first 10 visible characters. If the first user message
  has no text content, return no title.

## Data Ownership

Use `agent_sessions.metadata_json` to track title ownership. Avoid adding columns
unless this becomes a heavily queried feature.

Suggested metadata shape:

```json
{
  "title_source": "backend",
  "title_backend": "opencode",
  "title_native_session_id": "ses_...",
  "title_synced_at": "2026-06-02T07:40:00Z"
}
```

When the user edits a session title through the UI/API:

```json
{
  "title_source": "user",
  "title_user_modified_at": "2026-06-02T07:40:00Z"
}
```

Rules:

- `title_source=user` means backend title sync must never overwrite it.
- Empty `title` with missing/other source is eligible for backend backfill.
- Backend backfill sets `title_source=backend`.
- Once a non-empty title is written into Vibe, backend sync should not overwrite
  it. This keeps the rule simple: backfill empty titles only.
- Placeholder backend titles must not be written.

## Proposed Abstraction

Add a small backend title provider contract instead of duplicating storage logic
inside each agent implementation.

```python
@dataclass(slots=True)
class BackendSessionTitle:
    title: str
    source: Literal["backend", "derived_first_prompt"]
    confidence: Literal["high", "low"]


class BackendSessionTitleProvider(Protocol):
    def get_title(
        self,
        *,
        native_session_id: str,
        working_path: str,
        first_user_message: str = "",
    ) -> BackendSessionTitle | None:
        ...
```

Provider behavior:

- `OpenCodeSessionTitleProvider`: read `session.title` by native id, ignore
  default `New session - ...` and `Child session - ...`. When no usable
  backend title exists, derive from the first user message like Claude.
- `CodexSessionTitleProvider`: read `threads.title` by native thread id, ignore
  empty/default-looking values. Use `first_user_message` only if we explicitly
  choose derived fallback.
- `ClaudeSessionTitleProvider`: derive a fallback from the first user message's
  first 10 visible characters with `source="derived_first_prompt"` and
  `confidence="low"`.

The `derived_first_prompt` construction is shared:
`modules.agents.native_sessions.base.derive_first_prompt_title()` is the single
home for the truncated-first-message title, used by all three providers, so an
agent backend that never sets a title (observed with GLM on OpenCode, which
under-complies with the proactive title prompt) still gets a deterministic
backfilled title.

Implemented shape:

- `modules/agents/native_sessions/*` already contains backend-specific read
  logic for `/resume`, so each provider now exposes a focused `get_title()`.
- `core.session_titles.backfill_agent_session_title()` owns the shared
  orchestration: read first user text, ask the provider, call the storage writer,
  and publish `session.activity` after a successful write.
- `BaseAgent` only schedules best-effort title sync after successful terminal
  turns.

## Backfill Flow

Trigger title sync after the native session id is known and a turn reaches a
terminal state.

Recommended flow:

1. Agent creates or resumes native session.
2. Vibe Remote binds `native_session_id` to the reserved `agent_sessions` row.
3. User turn completes.
4. Agent calls a shared helper:

```python
maybe_backfill_agent_session_title(
    agent_session_id=...,
    backend=...,
    native_session_id=...,
    working_path=...,
)
```

5. Helper checks the Vibe session row:
   - no row: skip
   - non-empty title with `title_source=user`: skip
   - any non-empty title: skip
   - empty title: backfill if provider returns an eligible title
6. Persist title + metadata.
7. Publish a session update event so Workbench sidebar/header refreshes.

OpenCode timing:

- The semantic title may be generated shortly after the first prompt path runs.
- First call after turn completion should try once.
- If the provider returns no title because it is still the default placeholder,
  schedule one short retry after 3 seconds, without blocking the agent response.
- Do not create a long-running poll loop just for title sync.

## UI / API Behavior

Session update API:

- When `PATCH /api/sessions/<id>` changes `title`, mark metadata
  `title_source=user`.
- Clearing a title should be treated as user intent. Suggested behavior:
  - if user sets empty title explicitly, set `title_source=user` and keep title
    empty, so backend sync does not immediately refill it.
  - if we want "reset to backend title" later, add an explicit reset action.

Workbench display:

- Prefer `agent_sessions.title` when present.
- If empty, keep current fallback behavior.
- Do not display backend placeholder titles such as `New session - ...`.

Creation-path confirmation:

- Plain Avibe Workbench chat creation currently calls `api.createSession` with
  only `project_id`; it does not send the localized UI fallback
  `Untitled session` / `未命名会话`.
- Sidebar "new session" also sends only `project_id`.
- Chat header and sidebar render `Untitled session` / `未命名会话` only as a UI
  fallback when `session.title` is null/empty.
- The backend `create_session` path stores `title=None` when the incoming title
  is missing, empty, or whitespace-only.
- `CreateViaChatDialog` is a different, explicit task/watch setup flow and
  intentionally sends a business title such as Task / Watch; it is not the
  plain new-chat fallback.

## Tests

Unit coverage:

- OpenCode create-session no longer sends `title`.
- OpenCode provider ignores default `New session - ...`.
- OpenCode provider returns semantic `session.title`.
- Codex provider returns `threads.title`.
- Claude provider derives the first 10 visible characters from the first user
  message and marks the source as `derived_first_prompt`.
- Backfill writes title only when Vibe title is empty.
- Backfill does not overwrite `title_source=user`.
- Backfill does not overwrite an existing backend-owned title.
- User `PATCH title` marks metadata as user-owned.
- Plain Workbench create-session requests without a title persist
  `agent_sessions.title = NULL`; localized fallback strings never enter the DB.

Integration-ish tests:

- Workbench session first turn with OpenCode binds native id and eventually
  fills `agent_sessions.title` from OpenCode.
- Workbench session first turn with Codex fills from Codex native title when
  available.
- Claude session fills from the first user message's first 10 visible
  characters.

Regression scenarios:

- Existing IM thread with no Workbench title still replies normally.
- Existing Workbench session renamed by user is not overwritten by a later
  backend-generated title.
- OpenCode `/resume` list still shows OpenCode native titles through the native
  session provider.

## Implementation Status

Implemented in `feature/backend-session-title-backfill`:

1. Added backend title candidate API and provider implementations.
2. Stopped passing `title` to OpenCode `create_session`.
3. Added shared session-title backfill helper in `core.session_titles`.
4. Called the helper after successful terminal turns for OpenCode, Codex, and
   Claude.
5. Added Claude provider with the first-10-visible-characters fallback.
6. Updated session title PATCH path to mark user ownership, including explicit
   user clears.
7. Published session update events after successful backend backfill.
8. Added focused tests.

## Open Questions

- Should Codex fallback to `first_user_message` when `threads.title` is empty,
  or should only backend-native `title` count?
- Is `metadata_json` enough for title ownership, or should `title_source` become
  a column once filtering/sorting by title origin matters?
- Should OpenCode title sync use one short delayed retry, or wait for a later
  natural session refresh when the semantic title is not ready immediately?
