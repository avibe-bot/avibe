# Global Configuration and Platforms

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
      "default_reasoning_effort": null,
      "error_retry_limit": 1
    },
    "claude": {
      "enabled": true,
      "cli_path": "claude",
      "idle_timeout_seconds": 600
    },
    "codex": {
      "enabled": true,
      "cli_path": "codex",
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
      "transport_protocol": "auto",
      "auto_recovery": true,
      "optimization_profile": "balanced",
      "edge_ip_version": "4",
      "edge_bind_address": "",
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
