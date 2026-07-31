# Configuration Model (V2)

V2 stores all user data under `~/.vibe_remote/` as JSON files. The model preserves current functionality while separating user settings from session state.

## File Layout

- `~/.vibe_remote/config/config.json`
- `~/.vibe_remote/state/settings.json`
- `~/.vibe_remote/state/sessions.json`
- `~/.vibe_remote/logs/vibe_remote.log`

## config.json (Global Configuration)

### Top-level

- `mode`: `"saas"` | `"self_host"`
- `version`: config schema version (string)

### slack

- `bot_token`: Slack bot token (`xoxb-...`)
- `app_token`: Slack app token (`xapp-...`) (self-host only)
- `team_id`: Slack workspace ID (SaaS OAuth)
- `team_name`: Slack workspace name (SaaS OAuth)
- `app_id`: Slack app ID (optional, for diagnostics)
- `signing_secret`: optional for SaaS relay validation
- `disable_link_unfurl`: bool, suppress Slack link and media previews on outbound messages

### gateway

- `relay_url`: cloud relay endpoint (SaaS only)
- `workspace_token`: workspace binding token for relay auth
- `client_id`: local gateway identifier
- `client_secret`: local gateway secret or pairing token
- `last_connected_at`: last successful relay connect (ISO 8601)

### runtime

- `default_cwd`: default working directory
- `log_level`: `"DEBUG" | "INFO" | "WARNING" | "ERROR"`
- `require_mention`: bool (channel messages require @mention; stored under `slack`)
- `harness_run_sweep_interval_seconds`: int, default `60` — how often the Harness
  staleness sweep looks for runs nothing is executing any more. `0` disables the sweep
  entirely (and with it all three classes below).
- `harness_run_orphan_grace_seconds`: int, default `120` — how long a `running` agent run
  with no live owner is tolerated before it is settled `failed` with
  `interrupt_reason="orphaned"`. `0` disables this class.
- `harness_run_queued_ttl_seconds`: int, default `1800` — how long a run may sit `queued`
  because its chat platform never reconnected before it is settled `failed` with
  `interrupt_reason="transport_unavailable"`. Only rows the dispatcher actually recorded
  as transport-blocked are eligible; a run merely waiting on a busy session or on
  capacity is never affected. `0` disables this class.
- `harness_run_hold_ttl_seconds`: int, default `3600` — how long an untouched workbench
  queue hold is kept before its run is settled `failed` with
  `interrupt_reason="queue_hold_expired"`. Any queue activity resets the clock. `0`
  disables this class.

All four are read per sweep, so a config save takes effect on the next tick without a
restart. A missing or non-numeric value falls back to the default rather than failing
the tick. Design rationale and the evidence each class requires:
`docs/plans/agent-run-zombie-settlement.md` §4.

### agents

- `default_backend`: deprecated legacy field; ignored by current Agent-first routing.
- `opencode`:
  - `enabled`: bool
  - `cli_path`: absolute path to OpenCode CLI
  - `default_agent`: optional default OpenCode agent
  - `default_model`: optional default model
  - `default_reasoning_effort`: `"low" | "medium" | "high" | "xhigh"`
- `claude`:
  - `enabled`: bool
  - `cli_path`: absolute path to ClaudeCode CLI
  - `default_model`: optional model override
- `codex`:
  - `enabled`: bool
  - `cli_path`: absolute path to Codex CLI
  - `default_model`: optional model override

### ui

- `setup_host`: hostname/interface for local setup UI
- `setup_port`: port for local setup UI
- `open_browser`: bool

## settings.json (User/Channel Settings)

Settings are stored per channel and preserve existing functionality.

- `channels`: map keyed by `channel_id`
  - `show_message_types`: list (default: `[]` - hide all message types)
  - `custom_cwd`: optional cwd override
  - `routing`:
    - `agent_name`: optional Vibe Agent name
    - `model`: optional scope model override
    - `reasoning_effort`: optional scope reasoning override
    - `<backend>_agent`: optional backend-specific subagent override

The old scope backend route field is deprecated and ignored.

## sessions.json (Session State)

Session tracking is separated from settings and aligned with current behavior.

- `channels`: map keyed by `channel_id`
  - `session_mappings`: map of base session to path -> session_id (agent-aware)
  - `active_threads`: Slack thread tracking
  - `last_activity`: ISO 8601 timestamp

## Notes

- V2 does not read `.env` or legacy settings files.
- All keys are optional unless required by the selected mode.
- Default values should be applied by the loader if fields are missing.
