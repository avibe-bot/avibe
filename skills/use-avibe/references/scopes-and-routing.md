# Channels, Users, and Routing

### Channel settings

- `GET /settings?platform=<platform>`
  - returns channel settings, user settings, and bind codes for one platform
- `POST /settings`
  - payload: `{"platform": "<platform>", "channels": {...}}`
  - validates message visibility and routing, normalizes Claude reasoning, persists the full channel map for that platform

Important: `POST /settings` replaces the entire `channels` map for the selected platform. To change one channel:

1. `GET /settings?platform=<platform>`
2. copy `response.channels`
3. merge or add one channel entry
4. `POST /settings` with the full merged `channels` object
5. `GET /settings?platform=<platform>` again and verify

Channel entry shape:

```json
{
  "enabled": true,
  "show_message_types": ["assistant"],
  "custom_cwd": "/path/to/repo",
  "require_mention": null,
  "require_bind": null,
  "routing": {
    "agent_name": "codex",
    "model": "gpt-5.4",
    "reasoning_effort": "high",
    "opencode_agent": null,
    "opencode_model": null,
    "opencode_reasoning_effort": null,
    "claude_agent": null,
    "claude_model": null,
    "claude_reasoning_effort": null,
    "codex_agent": "reviewer",
    "codex_model": "gpt-5.4",
    "codex_reasoning_effort": "high"
  }
}
```

Field meanings:

- `enabled`: whether this channel is allowed to use Avibe
- `show_message_types`: visible intermediate messages; allowed values are `system`, `assistant`, `toolcall`
- `custom_cwd`: scope-level working directory override; empty string or `null` means use global default
- `require_mention`: `null` inherits the platform default, `true` requires mention, `false` disables mention gating for that channel
- `require_bind`: `null`/`false` lets any channel member use the bot (current default); `true` gates the channel to bound users only — messages from unbound senders are silently ignored (no denial reply), while the bot's own replies stay visible to everyone. Enforced in the shared auth pipeline, so it applies on every platform. Bind is platform-wide, so `require_bind` means "is this sender a bound user", not a per-channel allowlist.
- `routing.agent_name`: Vibe Agent name for this scope, or `null` to inherit the default Agent
- `routing.model`: canonical scope-level model override for the selected Agent backend
- `routing.reasoning_effort`: canonical scope-level reasoning override for the selected Agent backend
- `routing.<backend>_agent`: backend-specific subagent
- `routing.<backend>_model` / `routing.<backend>_reasoning_effort`: legacy aliases accepted on input and derived on read-back; do not treat them as independent state

### DM users and bind codes

- `GET /api/users?platform=<platform>`
  - returns bound DM users for one platform
- `POST /api/users`
  - payload: `{"platform": "<platform>", "users": {...}}`
  - merges included users into existing users and preserves each existing user's `dm_chat_id`
- `POST /api/users/<user_id>/admin`
  - payload: `{"platform": "<platform>", "is_admin": true}`
- `DELETE /api/users/<user_id>?platform=<platform>`
  - removes a bound user; this is the reliable way to revoke DM access
- `GET /api/bind-codes`
  - returns all bind codes
- `POST /api/bind-codes`
  - payload: `{"type": "one_time"}` or `{"type": "expiring", "expires_at": "2026-04-18"}`
- `DELETE /api/bind-codes/<code>`
  - deactivates a bind code
- `GET /api/setup/first-bind-code`
  - returns an existing valid setup bind code or creates a new one-time code

Important: user updates are not field patches. Before changing a user's routing, cwd, visibility, or enabled flag, read the current user object and send the merged full user entry.

User entry shape:

```json
{
  "display_name": "Alice",
  "is_admin": false,
  "bound_at": "2026-03-20T12:34:56+00:00",
  "enabled": true,
  "show_message_types": ["assistant"],
  "custom_cwd": "/path/to/repo",
  "routing": {
    "agent_name": "claude",
    "model": "claude-sonnet-4-6",
    "reasoning_effort": "high",
    "opencode_agent": null,
    "opencode_model": null,
    "opencode_reasoning_effort": null,
    "claude_agent": "reviewer",
    "claude_model": "claude-sonnet-4-6",
    "claude_reasoning_effort": "high",
    "codex_agent": null,
    "codex_model": null,
    "codex_reasoning_effort": null
  }
}
```

DM caveat: current DM authorization checks whether the user is bound, not whether `enabled` is true. If the user wants to revoke DM access, use `DELETE /api/users/<user_id>?platform=<platform>` instead of only setting `enabled` to false.

## Scope and Precedence Rules

### Agent selection

Agent resolution priority is:

1. existing Agent Session backend snapshot, when the message belongs to an existing session/thread
2. scope-level `routing.agent_name` from `/settings` or `/api/users`, for new sessions
3. global default Vibe Agent from the Agent catalog
4. registered backend compatibility fallback, only when no enabled default Agent is available

If the user names a specific channel or DM and wants a specific Agent, use the scope API, not global `/config`. New routing is Agent-based.

### Working directory

Working directory resolution is:

1. `custom_cwd` on the target channel or user scope
2. `runtime.default_cwd` from `/config`

### Message visibility

`show_message_types` is scope-local. Preserve existing values unless the user wants an explicit replacement.

If a user asks for "vault messages", "internal messages", or "tool execution messages", map that request to `show_message_types`. Current Avibe does not expose a separate `vault` field.

### Mention policy

`require_mention` works like this:

- `null`: inherit platform default from `/config`
- `true`: require mention in that channel
- `false`: do not require mention in that channel

## Recipes

### Route one Slack channel to Codex

Goal:

- enable Slack channel `C123`
- route it to Codex
- use Codex subagent `reviewer`
- use model `gpt-5.4`
- set reasoning `high`

API flow:

1. `GET /settings?platform=slack`
2. merge `channels.C123`
3. `POST /settings` with all Slack channels
4. read back `GET /settings?platform=slack`

Merged channel entry:

```json
{
  "enabled": true,
  "show_message_types": ["assistant"],
  "custom_cwd": null,
  "require_mention": null,
  "routing": {
    "agent_name": "codex",
    "model": "gpt-5.4",
    "reasoning_effort": "high",
    "opencode_agent": null,
    "opencode_model": null,
    "opencode_reasoning_effort": null,
    "claude_agent": null,
    "claude_model": null,
    "claude_reasoning_effort": null,
    "codex_agent": "reviewer",
    "codex_model": "gpt-5.4",
    "codex_reasoning_effort": "high"
  }
}
```

### Route one channel to OpenCode with a subagent

Use `/settings` and set:

- `routing.agent_name = "opencode"`
- `routing.opencode_agent = "<agent>"`
- `routing.model = "<model>"` if requested
- `routing.reasoning_effort = "<effort>"` if requested

If the user wants OpenCode-native defaults, providers, MCP servers, skills, plugins, or API credentials, use OpenCode config instead of Avibe scope routing.

### Route one scope to Claude with model and reasoning

Use `/settings` for a channel or `/api/users` for a DM user and set:

- `routing.agent_name = "claude"`
- `routing.claude_agent = "<agent>"` if requested
- `routing.model = "<model>"`
- `routing.reasoning_effort = "<effort>"`

The API normalizes Claude reasoning for incompatible model combinations; verify by reading back the saved payload.

### Show tool execution messages in one channel

Use `/settings` and add `toolcall` to the target channel's `show_message_types`.

Preserve existing `system` and `assistant` values unless the user asked for a full replacement.

### Generate a DM bind code

Use `POST /api/bind-codes`:

```json
{
  "type": "one_time"
}
```

For an expiring code:

```json
{
  "type": "expiring",
  "expires_at": "2026-04-18"
}
```

Do not expose bind codes unless the user explicitly asks for them.
