# Service Operations and Troubleshooting

## Runtime Layout

Avibe stores runtime data under `~/.avibe/` by default, or under `AVIBE_HOME` when that env var is set. Existing default `~/.vibe_remote/` homes may be migrated to `~/.avibe/` with `~/.vibe_remote` kept as a back-symlink. The only paths an agent normally needs:

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

- `vibe task add`, `vibe task update`, `vibe task list [--include-finished] [--page N] [--limit N]`, `vibe task show <id>`, `vibe task run <id>`, `vibe task pause <id>`, `vibe task resume <id>`, `vibe task remove <id>`
- Switching a task between message and command form, or changing `--on-failure`, is rejected — remove and recreate.

Agent runs:

- `vibe agent run` — run one Agent job now. Runs are async by default; pass `--sync` only when the CLI should wait.
- `vibe runs list`, `vibe runs show <id>`, `vibe runs cancel <id>` — inspect and manage concrete Agent Run records

Watches:

- `vibe watch add`, `vibe watch update <id>`, `vibe watch list [--include-finished] [--page N] [--limit N]`, `vibe watch show <id>`, `vibe watch pause <id>`, `vibe watch resume <id>`, `vibe watch remove <id>`

For any subcommand, prefer `<command> --help` before composing a new invocation. Harness commands default to this Agent Session inside an Avibe-injected Agent shell. Use `--session-id` only to target a different existing Session, `--same-scope` or `--scope-id` when creating a Session in a visible scope, and `--no-callback` only when an async Agent run will be inspected later through `vibe runs`. Use `--message` / `--message-file` for user messages. `vibe task add` and `vibe watch add` take `--name`; only `vibe task add` takes `--cron` / `--at` / `--timezone`; `vibe agent run` takes `--sync`; and `vibe watch add` takes its own waiter options (`--shell` or a positional command after `--`, `--cwd`, `--timeout`, `--forever`, `--lifetime-timeout`, `--retry-exit-code`, `--retry-delay`). `vibe task add` also accepts `--shell` or a command after `--`, plus `--on-failure {none,agent}` and a per-run `--timeout`; a pure command task takes no session, scope, or agent flags. Do not copy flags between task, watch, and agent-run commands without checking help.

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
