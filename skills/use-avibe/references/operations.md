# Service Operations and Troubleshooting

## Runtime Layout

Avibe stores runtime data under `~/.avibe/` by default, or under `AVIBE_HOME` when that env var is set. Existing default `~/.vibe_remote/` homes may be migrated to `~/.avibe/` with `~/.vibe_remote` kept as a back-symlink. The only paths an agent normally needs:

- `~/.avibe/config/config.json` — global config; mutate through `POST /api/config`, not by editing the file
- `~/.avibe/logs/vibe_remote.log` — main application log; read via `POST /api/logs`
- `~/.avibe/screenshots/` — default output directory for `vibe screenshot`

Agent harness state is managed through `vibe agent run`, `vibe task`, `vibe watch`, and `vibe runs` (or their API endpoints), not by editing persistence files. Everything else under `state/` and `runtime/` is internal — treat it as opaque.

## API Endpoint Reference

### Health and inspection

- `GET /health`
  - returns `{"status":"ok"}` when the Web UI server is reachable
- `GET /status`
  - returns runtime status, running state, PID metadata, and last action
- `GET /api/doctor`
  - reads the latest persisted doctor result
- `POST /api/doctor`
  - runs doctor immediately and returns the result
- `POST /api/logs`
  - payload: `{"lines": 500, "source": "service"}`
  - `source` can be `service` or another source listed in the response; use `all` for aggregated logs
- `GET /api/version`
  - returns current version and update metadata
- `GET /api/macos/login-session`
  - returns `{"ok": true, "state": ...}`, the service's macOS login-session state (see [macOS login session](#macos-login-session)); `not_applicable` off macOS
- `GET /api/csrf-token`
  - issues the `vibe_csrf_token` cookie and returns the matching token value for `X-Vibe-CSRF-Token`
- `GET /api/platforms`
  - returns the static catalog of supported IM platforms only (id, config_key, title/description i18n keys, credential field names, capabilities). It does not include enablement or credential-presence state — fetch `/api/config` to see which platforms are enabled and whether credentials are configured.

### Control endpoints

- `POST /api/control`
  - payload: `{"action": "start"}`, `{"action": "stop"}`, or `{"action": "restart"}`
- `POST /api/ui/reload`
  - payload: `{"host": "127.0.0.1", "port": 5123}`
- `POST /api/upgrade`
  - payload: `{}`
  - triggers an in-place upgrade to the latest released version using the same code path as `vibe upgrade`

Avoid these for routine configuration. `POST /api/control` starts, stops, or restarts the service. `POST /api/ui/reload` restarts only the Web UI server to apply host or port changes. `POST /api/upgrade` reinstalls Avibe and then restarts the service. Use them only with explicit user intent or a concrete need.

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
- `vibe autostart on | off | status` — launch at desktop login, macOS CLI installs only, off by default; see [Launch at login](#launch-at-login)

On macOS, a restart run from outside the desktop session (over SSH) behaves differently; see [macOS login session](#macos-login-session).

Updates:

- `vibe check-update` — query the release feed and print whether an upgrade is available
- `vibe upgrade` — install the latest release. When Avibe was running, the command schedules the restart onto the new version itself, as soon as the install succeeds, and prints `Restart scheduled to use the new version.` with a job ID; do not follow it with another `vibe restart`. There is no delayed form, so from inside an active conversation the restart can cut off the current reply. When Avibe was stopped, it stays stopped and the new version is used at the next start. Exit status 2 means the install succeeded but the restart could not be scheduled: run `vibe restart`. Inside a desktop app's Runtime it changes nothing; the app updates Avibe. `POST /api/upgrade` does the same.

Remote access:

- `vibe remote` — guided Avibe Cloud pairing
- `vibe remote pair <key>` — pair using an existing one-time key
- `vibe remote status [--json]` — show tunnel + OIDC state
- `vibe remote start` / `vibe remote stop` — manage the cloudflared tunnel after pairing

Screenshots:

- `vibe screenshot` — capture the local desktop to `~/.avibe/screenshots/`
- `vibe screenshot --output <path>` / `--json` — pick an explicit output path or get machine-readable output

Scheduled tasks:

- `vibe task add`, `vibe task update`, `vibe task list [--session-id <id> | --global] [--include-finished] [--page N] [--limit N]`, `vibe task show <id>`, `vibe task run <id>`, `vibe task pause <id>`, `vibe task resume <id>`, `vibe task remove <id>`
- Switching a task between message and command form, or changing `--on-failure`, is rejected — remove and recreate.

Agent runs:

- `vibe agent run` — run one Agent job now. Runs are async by default; pass `--sync` only when the CLI should wait.
- `vibe runs list [--session-id <id> | --global]`, `vibe runs show <id>`, `vibe runs cancel <id>` — inspect and manage concrete Agent Run records

Watches:

- `vibe watch add`, `vibe watch update <id>`, `vibe watch list [--session-id <id> | --global] [--include-finished] [--page N] [--limit N]`, `vibe watch show <id>`, `vibe watch pause <id>`, `vibe watch resume <id>`, `vibe watch remove <id>`

For any subcommand, prefer `<command> --help` before composing a new invocation. Harness commands default to this Agent Session inside an Avibe-injected Agent shell. Use `--session-id` only to target a different existing Session, `--same-scope` or `--scope-id` when creating a Session in a visible scope, and `--no-reply` with an existing Session or Bot when no answer or read receipt is wanted. Agent Run callback flags are deprecated no-ops; `--sync` still waits inline and failures still notify the sender. Use `--message` / `--message-file` for user messages. `vibe task add` and `vibe watch add` take `--name`; only `vibe task add` takes `--cron` / `--at` / `--timezone`; `vibe agent run` takes `--sync`; and `vibe watch add` takes its own waiter options (`--shell` or a positional command after `--`, `--cwd`, `--timeout`, `--forever`, `--lifetime-timeout`, `--retry-exit-code`, `--retry-delay`). `vibe task add` also accepts `--shell` or a command after `--`, plus `--on-failure {none,agent}` and a per-run `--timeout`; a pure command task takes no session, scope, or agent flags. Do not copy flags between task, watch, and agent-run commands without checking help.

## macOS login session

On macOS, Avibe and every Agent Session run in the login session of whatever started Avibe, and only the user's desktop session reaches the Keychain, where `gh` keeps its token and Vault its key. The service reports its state through `GET /api/macos/login-session` and Doctor:

| State | Shown as | Meaning |
| --- | --- | --- |
| `outside_gui_session` | Doctor `warn`, Web UI banner, Vault error code | The user is logged in at the desktop and Avibe runs in another login session, typically because it was started or restarted over SSH. Keychain credentials are unreachable: `gh` can report an invalid token and Vault cannot use its key, though both are intact. |
| `stale_login_session` | Doctor `fail`, Web UI banner, Vault error code | Avibe outlived the login session it started in (a log-out, or the desktop session ended). The Keychain and name resolution no longer work. |
| `no_gui_session` | Doctor `pass` | Nobody is logged in at the desktop; expected for headless use. |

### Repair

For either degraded state:

- CLI install: the user runs `vibe restart` in a terminal on the Mac's current desktop. Nothing needs to sign in or be authorized again.
- With [launch at login](#launch-at-login) on, the next desktop login restarts Avibe into that session, so logging out and in repairs it.
- Desktop app: the user quits and reopens the app, which replaces its own Runtime left in another login session.

For either state, do not retry the failing operation, and do not tell the user to run `gh auth login`, `vibe vault key authorize`, or Authorize avault: the credentials are not the problem. Do not restart Avibe yourself to fix it: a restart from an Agent shell, the Web UI, `POST /api/control`, or `POST /api/upgrade` runs in the service's own session and keeps it there. That same property keeps a healthy service in the desktop session.

### Restart from outside the desktop session

`vibe restart` (with or without `--delay-seconds`) and `vibe upgrade` run from outside the desktop session, such as over SSH, are handed to the running Avibe when it reports a `healthy` login session. The command then prints `The running Avibe, which is in this Mac's desktop session, performs the restart.` after the job ID, and Avibe comes back in the desktop session with its own launcher and environment; the caller's are not applied. Otherwise the restart still runs from the caller and prints `Warning: Avibe will run outside this Mac's desktop session ...`: the new service will report `outside_gui_session` and needs the repair above. That happens when no Avibe is running, the running one is not `healthy`, it predates this hand-over, or the caller's `vibe` is not the install the service restarts with (a virtualenv or source checkout). `vibe start` from outside the desktop session prints the same warning. Because an older Avibe cannot take the hand-over, the first restart onto a release that has it must come from the desktop or from an Agent shell of an Avibe already in the desktop session. A desktop app's Runtime is not affected.

### Launch at login

`vibe autostart on | off | status` (add `--json` for the envelope) manages one per-user LaunchAgent for a CLI install on macOS; it is off by default and the LaunchAgent file is the setting. At each desktop login it runs once: it starts Avibe when none is running, restarts an Avibe that outlived its login session or runs in another one, and otherwise does nothing, writing the reason to `~/.avibe/logs/autostart.log`. `on` also runs it once immediately, so it can restart Avibe at once. Nothing runs between logins: a stopped Avibe stays stopped, and nothing restarts an Avibe that exits. `off` removes only the LaunchAgent; a running Avibe keeps running.

An Avibe started this way has its own macOS privacy permissions, attributed to the Python interpreter that `vibe autostart status` names, not the terminal's. When workspaces are in protected folders or on external volumes, the user adds that interpreter under System Settings > Privacy & Security > Full Disk Access, and checks `status` again after an upgrade that changes the Python version. It is refused on Linux and Windows, inside a desktop app's Runtime (the app has its own launch at login), when `AVIBE_HOME` names another home, and when `vibe` runs from inside an install generation or a virtual environment. Turn it on or off only at the user's request.

## Troubleshooting

Start with evidence:

1. `GET /status`
2. `POST /api/doctor`
3. `POST /api/logs` with a small line count and focused source
4. read back `/api/config`, `/api/settings`, or `/api/users` for the affected scope

Common cases:

- config does not apply: verify the API read-back first; only restart if the changed field is startup-only
- backend missing: confirm backend is enabled, CLI path is executable, and `/api/cli/detect` finds it
- channel does not respond: verify `/api/settings?platform=<platform>` contains the channel and `enabled` is true
- wrong repository/cwd: inspect `custom_cwd` and `runtime.default_cwd`
- DM access denied: inspect `/api/users?platform=<platform>` and bind-code state
- platform cannot reach API: inspect `proxy_url` on that platform's config block; check logs for proxy/TLS errors; for SOCKS proxies confirm `aiohttp_socks` is installed
- remote URL is unreachable: `GET /api/remote-access/status` should show `running: true` and `binary_found: true`; if not, run `POST /api/doctor` and check the configured `cloudflared_path`
- remote session expired: instruct the user to re-sign in at the public URL (24h TTL with sliding renewal); use `POST /auth/logout` to clear a stale session on the current device
- upgrade did not apply: both `POST /api/upgrade` and `vibe upgrade` restart a running service after a successful install; read their restart result (`vibe upgrade` exit status 2: the restart was not scheduled, run `vibe restart`), then verify with `vibe status` that the new PID is running. A `generation_downgrade` object in `vibe status` means the running service is older than the activated install; restart through the launcher it names
- macOS credentials fail (`gh` reports an invalid token, Vault returns `outside_gui_session` or `stale_login_session`, or the Web UI shows a desktop-session banner): read `GET /api/macos/login-session` and follow [macOS login session](#macos-login-session)
- a tool is missing in Agent shells (`vibe`, `gh`, `uv` not found): on macOS and Linux a start or restart gives Avibe the login shell's `PATH` when the starter's lacks any of its entries (over SSH, from cron or launchd); only `PATH` is taken, so the tool must be on the login shell's `PATH`, and a restart applies a profile change. When the login shell does not answer within 10 seconds, Avibe starts with the caller's `PATH` and says so
- startup failure: use `GET /status`, `POST /api/doctor`, then inspect logs

Do not use `vibe restart`, `POST /api/control {"action":"restart"}`, or `POST /api/ui/reload` as a first response to config problems.

If a restart is still required and you are replying through an active Avibe conversation, use `vibe restart --delay-seconds 60` so the current reply can be delivered before the restart lands.
