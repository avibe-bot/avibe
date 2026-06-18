---
name: use-vibe-remote
slug: use-vibe-remote
description: Safely inspect and modify local Avibe configuration, routing, runtime settings, watches, scheduled tasks, Avibe Cloud remote access, and operational state.
version: 0.4.0
---

# Use Avibe

Use this skill when the user asks you to configure, repair, explain, or operate a local Avibe installation.

Typical requests include:

- enable a Slack, Discord, Telegram, Lark/Feishu, or WeChat scope
- route one channel or DM user to OpenCode, Claude, or Codex
- set a working directory for a channel or DM user
- choose a backend model, subagent, or reasoning level
- show or hide intermediate message types
- configure an outbound proxy (`proxy_url`) for an IM platform that cannot reach its API directly
- pair, start, stop, or inspect Avibe Cloud remote Web UI access
- create, update, inspect, pause, resume, or remove a managed background watch with `vibe watch`
- create, inspect, run, pause, resume, or remove a scheduled task with `vibe task`
- run a one-shot Agent job with `vibe agent run`, including async background runs
- inspect or cancel concrete Agent Run records with `vibe runs`
- check or apply Avibe updates (`vibe check-update`, `vibe upgrade`)
- inspect logs, run doctor, check service status, or explain where Avibe stores state
- decide whether a requested change belongs in Avibe config or in the host backend's own config

Follow this skill as an operations playbook for agents, not as end-user marketing copy.

## Core Rules

1. Prefer the Web UI API for Avibe configuration changes. Do not hand-edit config files for routine work.
2. Read current API state before mutating. Merge the user's requested change into the current payload.
3. Preserve unrelated scopes, platforms, users, and secrets.
4. Treat secrets as opaque. Do not print, invent, rotate, or overwrite tokens unless the user explicitly provides replacements.
5. Use the smallest viable API call and verify by reading back the API response.
6. For `POST /settings`, preserve every existing channel for that platform; the endpoint replaces the platform's channel map.
7. For `POST /api/users`, merge each edited user with its current user payload first; missing user fields are not a patch.
8. Make every persistent-state change through the Web UI API or the `vibe` CLI. Avibe's internal storage is opaque — do not read, query, or hand-edit it.
9. `POST /config` persists the new payload but does not restart running platform adapters by itself. When the change is platform credentials, `proxy_url`, or other transport-level settings, plan an explicit restart afterwards; prefer the delayed CLI form (`vibe restart --delay-seconds 60`) when triggering it from inside an active conversation. The only credential save that restarts on its own is the WeChat QR-login completion through `POST /wechat/qr_login/poll`.
10. Do not restart the service by default. Use `POST /doctor`, `GET /status`, and read-back checks first.
11. Only start, stop, restart, or reload Avibe when the user explicitly asks or when a change cannot take effect otherwise; explain why before doing it.
12. If an agent must restart Avibe from an active conversation, use `vibe restart --delay-seconds 60` so the current session can receive the reply before the restart lands.
13. Tell the user whether the change is global or scope-specific.

## API First Workflow

Use this order when changing Avibe configuration:

1. Determine the Web UI base URL.
   - Default is `http://127.0.0.1:5123`.
   - If the user has a custom UI host or port (from `ui.setup_host` / `ui.setup_port`), use that exact origin.
   - When Avibe Cloud remote access is active, the public origin (e.g. `https://<slug>.avibe.bot`) also speaks the same API and requires OIDC session cookies — prefer the local origin from the host running Avibe.
   - Check liveness with `GET /health` or `GET /status`.
2. Decide whether the request belongs in:
   - `POST /config` for global defaults, platform credentials, runtime config, agent defaults, UI config, remote-access provider settings, update policy, or global display toggles
   - `POST /settings` for channel-level routing, working directory, visibility, enablement, and mention policy
   - `/api/users` and `/api/bind-codes` for DM user binding and user-scope settings
   - `/remote-access/*` for Avibe Cloud pairing and tunnel control
   - host backend config instead of Avibe when the request is OpenCode, Claude Code, or Codex native behavior
3. Fetch the current state from the matching GET endpoint.
4. Merge the requested change in memory.
5. Send the mutating request through the Web UI API with CSRF protection.
6. Read back the changed resource and verify the effective payload.
7. Run `POST /doctor` only when the change affects runtime health, platform credentials, or backend availability.
8. Report the changed scope or global keys and whether a restart was avoided or still required.

## Calling the Web UI API

Mutating API calls require:

- same-origin `Origin` or `Referer` header
- CSRF cookie named `vibe_csrf_token`
- matching `X-Vibe-CSRF-Token` header

Use this local curl pattern:

```bash
BASE="http://127.0.0.1:5123"
COOKIE_JAR="$(mktemp)"
CSRF="$(
  curl -fsS -c "$COOKIE_JAR" "$BASE/api/csrf-token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])'
)"

curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" \
  -H "X-Vibe-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -X POST "$BASE/doctor" \
  --data '{}'
```

For `DELETE`, use the same cookie jar, `Origin`, and CSRF header.

When the Web UI is served through Avibe Cloud, the same calls require an authenticated OIDC session cookie issued by `/auth/callback`. Prefer hitting `127.0.0.1:5123` directly from the local machine for maintenance work.

Do not log full request bodies when they contain tokens or secrets.

### Reusable local API helper

For multi-step maintenance, use the bundled helper at `scripts/vibe_api.py` instead of hand-writing curl commands. The helper handles CSRF, same-origin headers, cookies, JSON encoding, and readable error output.

Resolve paths relative to this skill directory. If the skill is installed at `skills/use-vibe-remote`, run:

Usage examples:

```bash
export VIBE_UI_BASE="http://127.0.0.1:5123"

python3 skills/use-vibe-remote/scripts/vibe_api.py GET /health
python3 skills/use-vibe-remote/scripts/vibe_api.py GET '/settings?platform=slack'
python3 skills/use-vibe-remote/scripts/vibe_api.py POST /doctor '{}'
python3 skills/use-vibe-remote/scripts/vibe_api.py POST /config '{"show_duration":true}'
python3 skills/use-vibe-remote/scripts/vibe_api.py DELETE '/api/users/U123?platform=slack'
```

Payload can be passed as inline JSON, as `@payload.json`, or as `-` to read JSON from stdin.

For scope updates, still fetch and merge first:

```bash
API_HELPER="skills/use-vibe-remote/scripts/vibe_api.py"

python3 "$API_HELPER" GET '/settings?platform=slack' > /tmp/slack_settings.json
python3 - <<'PY'
import json
from pathlib import Path

settings = json.loads(Path("/tmp/slack_settings.json").read_text())
channels = settings.get("channels") or {}
channels["C123"] = {
    **channels.get("C123", {}),
    "enabled": True,
    "show_message_types": channels.get("C123", {}).get("show_message_types") or ["assistant"],
    "custom_cwd": channels.get("C123", {}).get("custom_cwd"),
    "require_mention": channels.get("C123", {}).get("require_mention"),
    "routing": {
        **(channels.get("C123", {}).get("routing") or {}),
        "agent_name": "codex",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "codex_model": "gpt-5.4",
        "codex_reasoning_effort": "high",
    },
}
Path("/tmp/slack_payload.json").write_text(json.dumps({"platform": "slack", "channels": channels}))
PY
python3 "$API_HELPER" POST /settings @/tmp/slack_payload.json
python3 "$API_HELPER" GET '/settings?platform=slack'
```

## Runtime Layout

Avibe stores runtime data under `~/.avibe/` by default (or `AVIBE_HOME` if set). For backward compatibility, `VIBE_REMOTE_HOME` is still honored when set, and existing default `~/.vibe_remote/` homes may be migrated to `~/.avibe/` with `~/.vibe_remote` kept as a back-symlink. The only paths an agent normally needs:

- `~/.avibe/config/config.json` — global config; mutate through `POST /config`, not by editing the file
- `~/.avibe/logs/vibe_remote.log` — main application log; read via `POST /logs`
- `~/.avibe/screenshots/` — default output directory for `vibe screenshot`
- `~/.avibe/state/user_preferences.md` — shared long-term preference file (safe to read and update)

Agent harness state is managed through `vibe agent run`, `vibe task`, `vibe watch`, and `vibe runs` (or their API endpoints), not by editing persistence files. Everything else under `state/` and `runtime/` is internal — treat it as opaque.

## API Endpoint Reference

### Health and inspection

- `GET /health`
  - returns `{"status":"ok"}` when the Web UI server is reachable
- `GET /status`
  - returns runtime status, running state, PID metadata, and last action
- `GET /doctor`
  - reads the latest persisted doctor result
- `POST /doctor`
  - runs doctor immediately and returns the result
- `POST /logs`
  - payload: `{"lines": 500, "source": "service"}`
  - `source` can be `service` or another source listed in the response; use `all` for aggregated logs
- `GET /version`
  - returns current version and update metadata
- `GET /api/csrf-token`
  - issues the `vibe_csrf_token` cookie and returns the matching token value for `X-Vibe-CSRF-Token`
- `GET /platforms`
  - returns the static catalog of supported IM platforms only (id, config_key, title/description i18n keys, credential field names, capabilities). It does not include enablement or credential-presence state — fetch `/config` to see which platforms are enabled and whether credentials are configured.

### Global config

- `GET /config`
  - returns the current V2 config payload
- `POST /config`
  - accepts a partial object, deep-merges it with current config, validates it through `V2Config.from_payload`, then persists it
  - use for platform credentials, enabled platforms, primary platform, runtime defaults, agent defaults, UI config, remote-access provider settings, update policy, and global toggles
  - the handler only persists and (for `remote_access`) reconciles the cloudflared tunnel; running platform adapters keep using their previous credentials and transport until a restart. Plan a `vibe restart --delay-seconds 60` after any credential, `proxy_url`, or transport-level change.

Important config payload shape:

```json
{
  "platform": "slack",
  "platforms": {
    "enabled": ["slack", "discord", "telegram", "lark", "wechat"],
    "primary": "slack"
  },
  "mode": "self_host",
  "version": "v2",
  "slack": {
    "bot_token": "xoxb-...",
    "app_token": "xapp-...",
    "signing_secret": "...",
    "team_id": "T...",
    "team_name": "...",
    "app_id": "A...",
    "require_mention": false,
    "disable_link_unfurl": false,
    "proxy_url": null
  },
  "discord": {
    "bot_token": "...",
    "application_id": "...",
    "require_mention": false,
    "thread_auto_archive_minutes": 10080,
    "guild_allowlist": null,
    "guild_denylist": null,
    "proxy_url": null
  },
  "telegram": {
    "bot_token": "123:abc",
    "require_mention": true,
    "forum_auto_topic": true,
    "use_webhook": false,
    "webhook_url": null,
    "webhook_secret_token": null,
    "allowed_chat_ids": null,
    "allowed_user_ids": null,
    "proxy_url": null
  },
  "lark": {
    "app_id": "...",
    "app_secret": "...",
    "require_mention": false,
    "domain": "feishu",
    "proxy_url": null
  },
  "wechat": {
    "bot_token": "...",
    "base_url": "https://ilinkai.weixin.qq.com",
    "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
    "require_mention": false,
    "proxy_url": null
  },
  "runtime": {
    "default_cwd": "/path/to/workdir",
    "log_level": "INFO"
  },
  "agents": {
    "opencode": {
      "enabled": true,
      "cli_path": "opencode",
      "default_agent": null,
      "default_model": null,
      "default_reasoning_effort": null,
      "error_retry_limit": 1
    },
    "claude": {
      "enabled": true,
      "cli_path": "claude",
      "default_model": null,
      "idle_timeout_seconds": 600
    },
    "codex": {
      "enabled": true,
      "cli_path": "codex",
      "default_model": null,
      "idle_timeout_seconds": 600
    }
  },
  "ui": {
    "setup_host": "127.0.0.1",
    "setup_port": 5123,
    "open_browser": true
  },
  "remote_access": {
    "provider": "vibe_cloud",
    "vibe_cloud": {
      "enabled": false,
      "backend_url": "https://avibe.bot",
      "public_url": "",
      "instance_id": "",
      "client_id": "",
      "issuer": "",
      "authorization_endpoint": "",
      "token_endpoint": "",
      "jwks_uri": "",
      "redirect_uri": "",
      "tunnel_token": "",
      "instance_secret": "",
      "session_secret": "",
      "cloudflared_path": "",
      "dev_login_hint": ""
    }
  },
  "update": {
    "auto_update": true,
    "check_interval_minutes": 60,
    "idle_minutes": 30,
    "notify_admins": true
  },
  "ack_mode": "typing",
  "language": "en",
  "show_duration": false,
  "include_time_info": true,
  "include_user_info": true,
  "reply_enhancements": true
}
```

Discord server access belongs to `/settings`, not `/config`. Store enabled
servers under `guilds`, next to channel settings:

```json
{
  "platform": "discord",
  "guilds": {
    "900740769198006293": { "enabled": true }
  },
  "channels": {
    "1067738479234138202": { "enabled": true }
  }
}
```

When switching the active platform, update `platforms.primary` and make sure `platforms.enabled` contains the new primary. Keep the legacy `platform` field aligned for readability, but `platforms.primary` is the real multi-platform source of truth.

Per-platform fields worth knowing about:

- every platform inherits `proxy_url` from the shared `BaseIMConfig`. Set it when the host machine cannot reach the upstream API directly. Accepts standard HTTP/HTTPS proxy URLs and any `socks*://` URL (`socks4`, `socks4a`, `socks5`, `socks5h`). SOCKS variants route through `aiohttp_socks`.
- `slack.disable_link_unfurl` suppresses link previews when posting messages.
- `discord.thread_auto_archive_minutes` must be one of `60`, `1440`, `4320`, or `10080`.
- `discord.guild_allowlist` / `guild_denylist` are legacy input lists; current runtime server access lives in `/settings` under `guilds`.
- `telegram.forum_auto_topic` enables automatic topic creation in forum chats; `use_webhook` plus `webhook_url` / `webhook_secret_token` switches Telegram delivery to the webhook transport.
- `telegram.allowed_chat_ids` / `allowed_user_ids` restrict which chats and users Telegram will respond to.
- `wechat.cdn_base_url` controls the CDN host used for fetching WeChat media; the default `novac2c.cdn.weixin.qq.com` is the official c2c CDN.
- `update.auto_update`, `check_interval_minutes`, and `idle_minutes` control unattended upgrades; `notify_admins` posts the upgrade announcement to bound admins.
- `ui.setup_host`, `setup_port`, and `open_browser` configure the local Web UI server; changing host or port requires `POST /ui/reload`.

Secret-bearing config fields that you should not print:

- `slack.bot_token`
- `slack.app_token`
- `slack.signing_secret`
- `discord.bot_token`
- `telegram.bot_token`
- `telegram.webhook_secret_token`
- `lark.app_id` (treat as a sensitive identifier)
- `lark.app_secret`
- `wechat.bot_token`
- `gateway.workspace_token`
- `gateway.client_secret`
- `remote_access.vibe_cloud.tunnel_token`
- `remote_access.vibe_cloud.instance_secret`
- `remote_access.vibe_cloud.session_secret`
- `remote_access.vibe_cloud.client_id`
- any `proxy_url` value that embeds credentials such as `user:pass@host`

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

### Platform discovery and validation

- `GET /slack/manifest`
  - returns Slack app manifest JSON for setup
- `POST /slack/auth_test`
  - payload: `{"bot_token": "xoxb-..."}`
- `POST /slack/channels`
  - payload: `{"bot_token": "xoxb-...", "browse_all": false}`
- `POST /discord/auth_test`
  - payload: `{"bot_token": "..."}`
- `POST /discord/guilds`
  - payload: `{"bot_token": "..."}`
- `POST /discord/channels`
  - payload: `{"bot_token": "...", "guild_id": "..."}`
- `POST /telegram/auth_test`
  - payload: `{"bot_token": "123:abc"}`
- `POST /telegram/chats`
  - payload: `{"include_private": false}`
- `POST /lark/auth_test`
  - payload: `{"app_id": "...", "app_secret": "...", "domain": "feishu"}`
- `POST /lark/chats`
  - payload: `{"app_id": "...", "app_secret": "...", "domain": "feishu"}`
- `POST /lark/temp_ws/start`
  - payload: `{"app_id": "...", "app_secret": "...", "domain": "feishu"}`
- `POST /lark/temp_ws/stop`
  - payload: `{}`
- `POST /wechat/qr_login/start`
  - payload: `{"base_url": "https://ilinkai.weixin.qq.com"}` or `{}`
- `POST /wechat/qr_login/poll`
  - payload: `{"session_key": "..."}`

WeChat QR login is special: when login is confirmed and a token is returned, the API auto-binds the WeChat user and schedules an internal service restart so the new token can take effect. Do not add an extra restart unless the user asks.

### Remote access (Avibe Cloud)

These endpoints drive the managed `avibe.bot` tunnel that exposes the local Web UI to other devices. They are paired with the `remote_access.vibe_cloud` block under `/config`.

- `GET /remote-access/status`
  - returns `enabled`, `paired`, `public_url`, `running` (tunnel up), `pid`, `pid_state`, plus `binary_found` / `binary_path` / `binary_version` for the resolved `cloudflared` executable. Use `running: true` to assert the tunnel is up.
- `POST /remote-access/vibe-cloud/pair`
  - payload: `{"pairing_key": "vrp_..."}`
  - exchanges the one-time key for an OIDC client, tunnel token, and persists the full `remote_access.vibe_cloud` block; on success Avibe launches the cloudflared tunnel
- `POST /remote-access/start`
  - payload: `{}`
  - starts the cloudflared tunnel using the persisted pairing config
- `POST /remote-access/stop`
  - payload: `{}`
  - stops the cloudflared tunnel; configuration is preserved so `start` can resume later
- `GET /auth/callback`
  - OIDC redirect target used by avibe.bot during sign-in. Browser-driven; do not call directly from automation.
- `GET /api/session`
  - always returns `200` with an auth-state payload, never `401`. Three shapes: `{"remote": false}` when remote access is not configured for this request, `{"remote": true, "authenticated": false}` when remote access is on but the caller has no valid session cookie, and `{"remote": true, "authenticated": true, "email": "..."}` when signed in. Check `authenticated` to gate behavior; do not poll for HTTP status codes.
- `POST /auth/logout`
  - clears the avibe.bot session cookie on this device. Does not stop the tunnel.

The session cookie is bound to the tunnel session and expires after roughly 24 hours; the server slides the TTL when activity reaches the half-life. Do not invent custom auth headers — rely on the existing cookie issued by `/auth/callback`.

Treat `tunnel_token`, `instance_secret`, `session_secret`, and `client_id` from `remote_access.vibe_cloud` as opaque secrets.

### Backend and local helper endpoints

- `GET /cli/detect?binary=<name-or-path>`
  - detects a CLI binary path
- `POST /agent/<name>/install`
  - `name` must be `opencode`, `claude`, or `codex`
- `POST /opencode/options`
  - payload: `{"cwd": "/path/to/repo"}`
  - returns OpenCode model, agent, and reasoning option data for that cwd
- `POST /opencode/setup-permission`
  - intentionally writes OpenCode native config to set `permission` to `allow`
- `GET /claude/agents?cwd=/path/to/repo`
- `GET /codex/agents?cwd=/path/to/repo`
- `GET /claude/models`
- `GET /codex/models`
- `POST /browse`
  - payload: `{"path": "~", "show_hidden": false}`

### Control endpoints

- `POST /control`
  - payload: `{"action": "start"}`, `{"action": "stop"}`, or `{"action": "restart"}`
- `POST /ui/reload`
  - payload: `{"host": "127.0.0.1", "port": 5123}`
- `POST /upgrade`
  - payload: `{}`
  - triggers an in-place upgrade to the latest released version using the same code path as `vibe upgrade`

Avoid these for routine configuration. `POST /control` starts, stops, or restarts the service. `POST /ui/reload` restarts only the Web UI server to apply host or port changes. `POST /upgrade` reinstalls Avibe and then restarts the service. Use them only with explicit user intent or a concrete need.

When the restart is initiated by an agent from an active conversation, use the CLI delayed form `vibe restart --delay-seconds 60` so the transport does not cut off the current reply.

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

### Change the global default working directory

Use `POST /config`:

```json
{
  "runtime": {
    "default_cwd": "/path/to/workdir"
  }
}
```

Do not overwrite scope-level `custom_cwd` entries.

### Show tool execution messages in one channel

Use `/settings` and add `toolcall` to the target channel's `show_message_types`.

Preserve existing `system` and `assistant` values unless the user asked for a full replacement.

### Switch primary platform

Use `POST /config` and keep `platforms.enabled` complete:

```json
{
  "platform": "discord",
  "platforms": {
    "enabled": ["slack", "discord"],
    "primary": "discord"
  }
}
```

Make sure the target platform config section exists and validates. Do not delete old platform config unless the user explicitly asks.

### Configure an outbound proxy for an IM platform

When the host cannot reach a platform API directly, set `proxy_url` on that platform's config block.

Use `POST /config` with only the proxy field for the affected platform:

```json
{
  "telegram": {
    "proxy_url": "http://proxy.internal:3128"
  }
}
```

Notes:

- `proxy_url` accepts `http://`, `https://`, and any `socks*://` scheme (`socks4`, `socks4a`, `socks5`, `socks5h`). The SOCKS variants route through `aiohttp_socks` (bundled).
- Set the field to `null` (or omit it on a fresh save) to disable the proxy.
- `POST /config` only persists the new value; running platform adapters keep their old transport until the service restarts. After saving, run `vibe restart --delay-seconds 60` (or `POST /control {"action":"restart"}` with the user's confirmation) so the proxy applies to live connections.
- Do not paste credentialed proxy URLs (`user:pass@host`) into logs or chat replies; mask the credentials portion when reporting back.

### Pair Avibe Cloud remote access

Goal: connect the local Web UI to `avibe.bot` so it is reachable from another device.

1. The user signs in at `https://avibe.bot`, creates a remote-access bot, and copies the one-time pairing key (format `vrp_...`).
2. Call `POST /remote-access/vibe-cloud/pair` with `{"pairing_key": "vrp_..."}` from the local Web UI origin.
3. Verify with `GET /remote-access/status` — `enabled: true`, `paired: true`, `public_url` populated, `running: true`.
4. Have the user open `public_url` and sign in with the same avibe.bot account.

Alternatively, drive the same flow from the CLI:

```bash
vibe remote                       # guided flow
vibe remote pair vrp_abc123       # paste key directly
vibe remote status --json         # inspect tunnel state
vibe remote stop                  # stop tunnel; keep config
vibe remote start                 # bring tunnel back up
```

Treat the pairing key, tunnel token, instance secret, and session secret as opaque. Never echo them in chat replies.

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

## Agent Harness: Runs, Tasks, and Watches

Use the harness commands when the user wants an agent to leave the current turn and continue later, repeatedly, or in the background. The mental model is:

- `vibe agent run`: run one concrete Agent job now; add `--async` when it should continue in the background
- `vibe task`: save a time trigger that creates Agent Runs later
- `vibe watch`: save a condition trigger that waits for a process, file, log, CI, review, or other signal, then creates a follow-up Agent Run
- `vibe runs`: inspect and cancel concrete run records

Agents should prefer these managed harness commands over ad-hoc detached shells when the work should be inspectable, resumable, or report back to the conversation.

Preferred CLI shape:

- one-shot direct run: `vibe agent run --agent '<agent-name>' --message '...'`
- one-shot async run: `vibe agent run --async --session-id '<session-id>' --message '...'`
- fork a Session for an alternate path: `vibe agent run --fork-session '<source-session-id>' --message '...'`
- recurring task: `vibe task add --session-id '<session-id>' --cron '<expr>' --message '...'`
- one-off task: `vibe task add --session-id '<session-id>' --at '<ISO-8601>' --message '...'`
- immediate rerun: `vibe task run <id>`
- managed background watch: `vibe watch add --session-id '<session-id>' --message '...' -- <cmd>` (or `--shell '<cmd>'` to pass a single shell string)
- update a watch: `vibe watch update <id> --name '...' --timeout 1200`
- inspect a run: `vibe runs show <run-id>`
- cancel a run: `vibe runs cancel <run-id>`

Delivery controls (apply to `vibe agent run --create-session`, `vibe task add`, and `vibe watch add`):

- `--session-id` controls which Agent Session Avibe continues using
- when you want to keep the current session, use the current Agent Session ID
- if no usable Agent Session ID is available, confirm the target session first instead of guessing
- use `--post-to channel` when the task or watch should keep the same Agent Session but publish to the parent channel
- use `--deliver-key '<key>'` only when delivery must go to a different explicit target than the continued session
- do not combine `--post-to` and `--deliver-key` in the same command
- `--message` and `--message-file` are the current user-message flags for task, watch, and agent-run commands
- `vibe task add` stores the message template and creates Agent Runs when the time trigger fires
- `vibe agent run --async` queues one Agent Run immediately without storing a task definition
- `--fork-session` creates a new Agent Session by forking the source Session's native backend context. Use it when work should branch from an existing context without mutating that source Session.
- fork overrides are intentionally narrow: `--agent`, `--model`, and `--reasoning-effort` can override the forked Session only if the backend stays the same; cross-backend forks are rejected.
- do not combine `--fork-session` with `--session-id`, `--create-session`, `--deliver-key`, or `--post-to`.
- `vibe watch add` uses `--message` as the instruction template for the Agent Run created after the waiter reaches a reportable state

Legacy compatibility:

- `--session-key` is still accepted for old scripts; do not use it in new examples or instructions unless the user explicitly asks for legacy targeting
- `--prompt` / `--prompt-file` are deprecated compatibility inputs. Do not use them in new invocations; use `--message` / `--message-file`.
- `vibe hook send` is deprecated. Do not use it for new one-shot async work; use `vibe agent run --async`.

Operational guidance:

- use `vibe task list` before editing or deleting an existing task; use `vibe watch list` before touching a managed watch
- if this is the first time using `vibe task add`, `vibe agent run`, `vibe runs`, or `vibe watch add`, read the matching `--help` output first — watches accept additional flags like `--shell`, `--timeout` (per-cycle), `--lifetime-timeout` (overall), `--forever`, `--retry-exit-code`, and `--retry-delay`
- use `vibe task update <id>` to keep the same task ID while changing name, schedule, message, agent, or target
- use `vibe watch update <id> ...` when you must rename, retarget, or change the waiter/options
- use `vibe task list --brief` and `vibe watch list --brief` for scheduling-focused summaries
- `vibe task list` hides completed one-shot tasks by default; use `vibe task list --all` when you need full history
- use `vibe task show <id>`, `vibe watch show <id>`, or `vibe runs show <run-id>` to inspect stored fields and runtime state
- use `vibe task pause` / `vibe task resume` and `vibe watch pause` / `vibe watch resume` to disable a task or watch without deleting it
- treat `warnings` from task, watch, agent-run, or runs commands as delivery-risk hints to fix proactively

## Agent Backend Capability Matrix

Current Avibe Agent capabilities are:

| Backend | Select through Vibe Agent | Subagent | Model | Reasoning |
| --- | --- | --- | --- | --- |
| OpenCode | yes | yes | yes | yes |
| Claude | yes | yes | yes | yes |
| Codex | yes | yes | yes | yes |

Behavior notes:

- OpenCode subagents are selected through `routing.opencode_agent` or through prefix routing such as `reviewer: ...`.
- Claude subagents are selected through `routing.claude_agent` or prefix routing.
- Codex subagents are selected through `routing.codex_agent` or prefix routing.
- Claude reasoning is selected through `routing.claude_reasoning_effort`; common values are `low`, `medium`, and `high`, and some models also allow `max`.
- If a Claude reasoning value is invalid for the chosen model, the API normalizes or drops that override and falls back to the backend default.

## Subagent and Prefix Routing

If the user asks for subagents, remember:

- OpenCode, Claude, and Codex support prefix-triggered subagent selection like `planner: draft a migration plan`
- when a subagent definition provides its own default model or reasoning setting, that subagent-level value overrides the channel default
- Claude subagents are discovered from markdown files under:
  - `~/.claude/agents/`
  - project `.claude/agents/`
- Codex custom agents are discovered from TOML files under:
  - `~/.codex/agents/`
  - project `.codex/agents/`
- OpenCode subagent and model defaults come from the OpenCode runtime/config rather than only from Avibe's own config

## Host Backend Guidance

When the request belongs to the host backend, do not force it into Avibe config.

### OpenCode

Use OpenCode-native config when the user wants to change:

- personal default model
- global reasoning behavior
- provider and API keys
- MCP servers
- skills, plugins, tools, or project-local OpenCode behavior

Important locations:

- `~/.config/opencode/opencode.json`: global OpenCode config
- project `opencode.json`: project-level OpenCode config file
- `.opencode/`: project-local OpenCode config directory
- `~/.config/opencode/agents/`: global OpenCode agents
- `.opencode/agents/`: project-local OpenCode agents
- `~/.config/opencode/skills/`: global OpenCode skills
- `.opencode/skills/`: project-local OpenCode skills

Relevant docs:

- config: `https://opencode.ai/docs/config/`
- skills: `https://opencode.ai/docs/skills`
- plugins: `https://opencode.ai/docs/plugins/`
- MCP servers: `https://opencode.ai/docs/mcp-servers/`

Inside Avibe, a scope can select an OpenCode-backed Vibe Agent and then set subagent, model, and reasoning effort overrides. Use `POST /opencode/setup-permission` only for the specific permission helper.

### Claude Code

Use Claude-native config when the user wants to change:

- Claude subagent definitions
- Claude skills
- CLAUDE instructions and project rules

Important locations:

- `~/.claude/agents/`: global Claude subagents
- `.claude/agents/`: project subagents
- `~/.claude/skills/`: global Claude skills
- `.claude/skills/`: project skills

Relevant docs:

- subagents: `https://docs.anthropic.com/en/docs/claude-code/sub-agents`

Inside Avibe, a scope can select a Claude-backed Vibe Agent and then set model, subagent, and reasoning effort overrides.

### Codex

Use Codex-native config when the user wants to change:

- personal default model
- global reasoning defaults
- MCP servers, approvals, or sandbox policy
- Codex CLI profiles and behavior outside Avibe

Important locations:

- `~/.codex/config.toml`: global Codex config
- `.codex/config.toml`: project-local Codex config
- `~/.codex/agents/`: global Codex custom agents
- `.codex/agents/`: project-local Codex custom agents

Relevant docs:

- config basics: `https://developers.openai.com/codex/config-basic/`
- config reference: `https://developers.openai.com/codex/config-reference/`
- CLI overview: `https://developers.openai.com/codex/cli`
- subagents: `https://developers.openai.com/codex/subagents`

Inside Avibe, a scope can select a Codex-backed Vibe Agent and then set subagent, model, and reasoning effort overrides.

## CLI Reference

Use the CLI only when the Web UI API cannot cover the request, when the user explicitly asks for the command, or when restarting/upgrading from an active conversation.

Service lifecycle:

- `vibe` — start Avibe if needed and open the local Web UI; it does not stop an already-running service
- `vibe status` — print service status, PID metadata, and last action
- `vibe stop` — stop main service, Web UI, and any background helpers
- `vibe restart` — stop and re-start the service. Pass `--delay-seconds N` when triggering from inside an active conversation so the current reply has time to deliver before the restart lands.
- `vibe doctor` — run diagnostics and print the latest result
- `vibe version` — print the installed version

Updates:

- `vibe check-update` — query the release feed and print whether an upgrade is available
- `vibe upgrade` — reinstall Avibe to the latest release. The CLI does not restart the service for you; it prints "Please restart vibe..." and exits. Run `vibe restart` (or `vibe restart --delay-seconds 60` from inside an active conversation) yourself after the upgrade reports success. The Web UI's `POST /upgrade` endpoint is the path that performs an automatic restart.

Remote access:

- `vibe remote` — guided Avibe Cloud pairing
- `vibe remote pair <key>` — pair using an existing one-time key
- `vibe remote status [--json]` — show tunnel + OIDC state
- `vibe remote start` / `vibe remote stop` — manage the cloudflared tunnel after pairing

Screenshots:

- `vibe screenshot` — capture the local desktop to `~/.avibe/screenshots/`
- `vibe screenshot --output <path>` / `--json` — pick an explicit output path or get machine-readable output

Scheduled tasks:

- `vibe task add`, `vibe task update`, `vibe task list [--all|--brief]`, `vibe task show <id>`, `vibe task run <id>`, `vibe task pause <id>`, `vibe task resume <id>`, `vibe task remove <id>`

Agent runs:

- `vibe agent run` — run one Agent job now; add `--async` for one-shot background work
- `vibe runs list`, `vibe runs show <id>`, `vibe runs cancel <id>` — inspect and manage concrete Agent Run records

Watches:

- `vibe watch add`, `vibe watch update <id>`, `vibe watch list [--brief]`, `vibe watch show <id>`, `vibe watch pause <id>`, `vibe watch resume <id>`, `vibe watch remove <id>`

For any subcommand, prefer `<command> --help` before composing a new invocation. The delivery flags shared by harness commands are `--session-id`, `--post-to`, and `--deliver-key`, with command-specific mutual exclusion rules. Use `--message` / `--message-file` for user messages. `vibe task add` and `vibe watch add` take `--name`; only `vibe task add` takes `--cron` / `--at` / `--timezone`; `vibe agent run` takes `--async`; and `vibe watch add` takes its own waiter options (`--shell` or a positional command after `--`, `--cwd`, `--timeout`, `--forever`, `--lifetime-timeout`, `--retry-exit-code`, `--retry-delay`). Do not copy flags between task, watch, and agent-run commands without checking help.

## Troubleshooting

Start with evidence:

1. `GET /status`
2. `POST /doctor`
3. `POST /logs` with a small line count and focused source
4. read back `/config`, `/settings`, or `/api/users` for the affected scope

Common cases:

- config does not apply: verify the API read-back first; only restart if the changed field is startup-only
- backend missing: confirm backend is enabled, CLI path is executable, and `/cli/detect` finds it
- channel does not respond: verify `/settings?platform=<platform>` contains the channel and `enabled` is true
- wrong repository/cwd: inspect `custom_cwd` and `runtime.default_cwd`
- DM access denied: inspect `/api/users?platform=<platform>` and bind-code state
- platform cannot reach API: inspect `proxy_url` on that platform's config block; check logs for proxy/TLS errors; for SOCKS proxies confirm `aiohttp_socks` is installed
- remote URL is unreachable: `GET /remote-access/status` should show `running: true` and `binary_found: true`; if not, run `POST /doctor` and check the configured `cloudflared_path`
- remote session expired: instruct the user to re-sign in at the public URL (24h TTL with sliding renewal); use `POST /auth/logout` to clear a stale session on the current device
- upgrade did not apply: inspect the response from `POST /upgrade` (auto-restart on success) or `vibe upgrade` (does not auto-restart — run `vibe restart` manually), then verify with `vibe status` that the new PID is running
- startup failure: use `GET /status`, `POST /doctor`, then inspect logs

Do not use `vibe restart`, `POST /control {"action":"restart"}`, or `POST /ui/reload` as a first response to config problems.

If a restart is still required and you are replying through an active Avibe conversation, use `vibe restart --delay-seconds 60` so the current reply can be delivered before the restart lands.

## Safety Boundaries

Always follow these constraints:

- never delete unrelated platform scopes
- never blank out tokens or secrets as part of an unrelated config task
- never claim a backend feature exists if current Avibe behavior does not support it
- never read, query, or hand-edit Avibe's internal state storage — go through the Web UI API or `vibe` CLI
- never expose bind codes, pairing keys, tunnel tokens, instance secrets, or session secrets unless the user explicitly asks
- never paste a credentialed `proxy_url` (`user:pass@host`) back into chat — mask the credentials portion when echoing the value
- always say when a requested change actually belongs in OpenCode, Claude Code, or Codex config instead of Avibe

## Escalation

If the user still cannot solve a problem after API read-back checks, doctor, and log inspection, point them to the Avibe repository:

- repo: `https://github.com/avibe-bot/avibe`

Use that link when:

- the behavior looks like a real bug rather than a local misconfiguration
- the user is asking for a feature Avibe does not support yet
- backend integration behavior appears inconsistent with the documented configuration surface

If the user wants to contribute back, suggest opening an issue or a pull request in that repository.

## Response Pattern

When you complete an Avibe maintenance task, report back with:

1. which API endpoint changed the state
2. whether the change is global or scope-specific
3. which keys changed
4. the read-back or doctor evidence
5. whether a restart was avoided, deferred, or still required and why
