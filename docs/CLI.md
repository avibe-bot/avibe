# Avibe CLI Reference

## Quick Start

```bash
vibe              # Alias for vibe start
vibe start        # Start Avibe if needed (opens web UI)
vibe status       # Check service status
vibe restart      # Restart all services (use --delay-seconds when agent-triggered)
vibe remote       # Guided Avibe Cloud remote-access setup
vibe screenshot   # Capture a local desktop screenshot
vibe stop         # Stop all services
```

## Commands

## Remote Web UI Access

By default, the Web UI binds to `127.0.0.1:5123` on the machine where Avibe is running.

If you want to open the Web UI from another device, or you installed Avibe on a remote server, use the guided remote-access setup:

```bash
vibe remote
```

The command walks you through signing in at `https://avibe.bot`, creating a remote-access bot, claiming your personal domain, pasting the one-time pairing key, and starting the secure tunnel.


### `vibe`

Alias for `vibe start`.

```bash
vibe
```

**Behavior:**
- Starts Avibe if needed
- Reuses already-running processes
- Opens the web UI in your browser

### `vibe start`

Start Avibe if needed. Opens the web UI in your browser.

```bash
vibe start
```

**Behavior:**
- Reuses the main service and Web UI if they are already running
- Opens the setup wizard at `http://127.0.0.1:5123`
- **Preserves running processes** — Use `vibe restart` when you need an explicit restart

### `vibe stop`

Fully stop all Avibe services.

```bash
vibe stop
```

**Behavior:**
- Stops the main service
- Stops the web UI server
- **Terminates OpenCode server** — Use this when you need to restart OpenCode

### `vibe restart`

Restart Avibe (main service + Web UI). The OpenCode server is terminated as part of the restart.

```bash
vibe restart
vibe restart --delay-seconds 60
```

**Behavior:**
- Stops the main service and Web UI, then re-starts them
- Terminates the OpenCode server
- With `--delay-seconds N`, schedules the restart `N` seconds in the future so an active conversation can receive its reply before the restart lands. Prefer this form when an agent is triggering the restart from inside Slack, Discord, Telegram, Lark/Feishu, or WeChat.

### `vibe status`

Display current service status.

```bash
vibe status
```

**Output:**
```json
{
  "state": "running",
  "running": true,
  "pid": 12345
}
```

### `vibe doctor`

Run diagnostic checks on your configuration.

```bash
vibe doctor
```

**Checks:**
- Configuration file validity
- Slack token configuration
- Agent CLI availability (Claude Code, OpenCode, Codex)
- Runtime environment

### `vibe remote`

Start the guided Avibe Cloud remote-access setup.

```bash
vibe remote
```

**Flow:**
- The CLI explains what remote access does before asking for anything.
- Open `https://avibe.bot`, sign up or log in, create a new remote-access bot, claim your personal domain, and copy the one-time pairing key.
- Press Enter in the CLI, paste the pairing key, and Avibe saves the config and starts the managed tunnel automatically.
- On success, the CLI prints your remote URL and the next commands for checking or stopping the tunnel. When you open the URL, sign in with the same avibe.bot account.

If you already have a pairing key and want to skip the guided copy, use:

```bash
vibe remote pair vrp_abc123
```

Useful follow-up commands:

```bash
vibe remote status
vibe remote start
vibe remote stop
```

Use `--json` on these subcommands for machine-readable output.

### `vibe screenshot`

Capture the local desktop as a PNG file.

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

**Behavior:**
- Saves to `~/.vibe_remote/screenshots/` by default
- Prints the saved file path, or a JSON payload with `--json`
- Stays at the CLI layer only; it does not add IM commands, bot buttons, or agent prompt injection

### `vibe session`

List, inspect, and rename Agent sessions. `list` and `get` are read-only; `update`
changes the title only. Archived sessions are soft-deleted and never surfaced.

```bash
vibe session list                       # active sessions, 10 per page, newest activity first
vibe session list --type slack          # filter by platform (avibe = Web/Workbench)
vibe session list --page 2              # next page (fixed 10 per page; there is no --limit)
vibe session get sesk8m4q2p7x           # full detail for one session
vibe session update sesk8m4q2p7x --title 'Release review'   # pass "" to clear the title
```

`--type` accepts a platform id: `avibe` (Web/Workbench), `slack`, `discord`,
`telegram`, `lark`, `wechat`. For richer filtering — by agent, time range, message
content, or cross-table joins — `list` and `get` point you to `vibe data query`.

### `vibe task`

Create, inspect, update, run, pause, resume, or remove scheduled tasks.

```bash
vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
vibe task add --cron '0 * * * *' --message 'Share the hourly summary.'   # inside an Avibe Agent shell
vibe task list --brief
vibe task update <task-id> --cron '*/30 * * * *'
vibe task run <task-id>
vibe task remove <task-id>
```

Use `vibe task add --help` and `vibe task update --help` for the full command surface, including:

- `--session-id` for Agent Session continuity
- `--post-to channel` to publish into the parent channel while keeping thread context
- `--deliver-key` for an explicit delivery target
- `--cron` and `--at` scheduling
- `--name`, `--timezone`, and message file support

When `vibe task add` runs inside an Avibe-injected Agent shell, `--session-id`
may be omitted. Avibe defaults the task target to the caller Session from
`AVIBE_SESSION_ID` and reports that default in the command output. Explicit
`--session-id`, session creation flags, and delivery flags still win.

`--session-key` remains accepted for older scripts, but new tasks should use
the Agent Session ID shown in the active Avibe prompt.

### `vibe agent run`

Run an Agent directly. Use `--async` for a queued background run without storing
a scheduled task definition.

```bash
vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --async --no-callback --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --async --no-callback --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --async --session-id sesworker123 --callback-session-id sescaller456 --message 'Run the delegated investigation.'
vibe agent run --async --no-callback --create-session --deliver-key slack::channel::C999 --agent release-reviewer --message 'Post the deployment summary.'
```

Use `--fork-session <session-id>` when a new Agent Session should branch from
an existing Session's native backend context instead of starting blank. The new
Session keeps the source backend. `--agent`, `--model`, and
`--reasoning-effort` can override the forked Session only when the Agent backend
stays the same; a cross-backend fork is rejected. Do not combine
`--fork-session` with `--session-id`, `--create-session`, `--deliver-key`, or
`--post-to`.

Async runs need an explicit callback policy unless the command is running inside
an Avibe-injected Agent environment. Use `--callback-session-id` when the final
result text should return to a caller Session as a follow-up Agent message; use
`--no-callback` when you intentionally want to inspect the run later with
`vibe runs show` or by listing/polling runs. Agent-initiated Harness calls
default the callback to the current caller Session. The callback is independent
from ordinary delivery: if the target run also posts to its IM scope, the caller
Session still receives the result. Process messages such as system notes, tool
calls, and intermediate assistant updates are not included.

`vibe hook send` is kept only as a deprecated compatibility entrypoint. New
automation should use `vibe agent run`.

### `vibe watch`

Create, update, inspect, pause, resume, or remove a managed background watch. A watch
runs a long-lived waiter command (for example a build or a status poll) and,
when the command reaches a reportable state, combines `--message` with the
captured stdout and creates a follow-up Agent Run through the chosen session.

```bash
vibe watch add \
  --session-id sesk8m4q2p7x \
  --message 'Test run finished. Summarize the failures and propose next steps.' \
  -- ./scripts/run_tests.sh

vibe watch add \
  --message 'Test run finished. Summarize the failures and propose next steps.' \
  -- ./scripts/run_tests.sh     # inside an Avibe Agent shell

# Alternative: pass the command through a shell with --shell
vibe watch add \
  --session-id sesk8m4q2p7x \
  --message 'Build done. Summarize.' \
  --shell 'make build && ./scripts/post_build.sh'

vibe watch list --brief
vibe watch show <watch-id>
vibe watch update <watch-id> --name 'Watch deployment' --timeout 1200
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

The waiter command is passed positionally after `--` (or as a single shell
string via `--shell`). Use `vibe watch add --help` for the full surface,
including `--timeout` (per-cycle timeout in seconds), `--lifetime-timeout`
(total wall-clock limit), `--forever`, `--retry-exit-code`, `--retry-delay`,
`--post-to channel`, `--deliver-key`, and `--name`. Watches share
`--session-id`, `--post-to`, and `--deliver-key` semantics with `vibe task`
and `vibe agent run`. `vibe watch remove` hides the watch from management
views while preserving existing run history in SQLite. Prefer `vibe watch`
over ad-hoc `nohup` jobs when the
user wants a managed background task with a guaranteed follow-up message.

### `vibe version`

Show the installed version.

```bash
vibe version
```

### `vibe check-update`

Check if a newer version is available.

```bash
vibe check-update
```

### `vibe upgrade`

Upgrade to the latest version.

```bash
vibe upgrade
```

If Avibe is already running, the command schedules a managed restart so the
service and Web UI switch to the upgraded code. If Avibe is stopped, the command
keeps it stopped and the new version is used on the next start.

## Service Lifecycle

### Understanding "Restart" vs "Stop"

Avibe manages two types of processes:

| Process | Description |
|---------|-------------|
| **Main Service** | Handles chat platform communication and routes messages to agents |
| **OpenCode Server** | Backend server for OpenCode agent (if enabled) |

The key difference between commands:

| Command | Main Service | OpenCode Server |
|---------|--------------|-----------------|
| `vibe` | Start/reuse | Preserved |
| `vibe start` | Start/reuse | Preserved |
| `vibe restart` | Restart | **Terminated** |
| `vibe stop` | Stop | **Terminated** |

### Why This Matters

When you run `vibe restart`:
- The main service restarts cleanly
- The UI restarts too
- The OpenCode server is terminated as part of the restart

When you run `vibe stop`:
- **Everything stops cleanly**
- OpenCode server is terminated
- Use this before updating OpenCode or its configuration

## Common Scenarios

### Daily Restart

If an agent is triggering the restart from an active conversation, prefer the delayed form for a better user experience:

```bash
vibe restart --delay-seconds 60
```

Just want to restart Avibe immediately:

```bash
vibe restart
```

### Update OpenCode Configuration

After editing `~/.config/opencode/opencode.json`:

```bash
vibe restart --delay-seconds 60
```

### Update OpenCode Binary

After installing a new version of OpenCode:

```bash
vibe restart --delay-seconds 60
```

### Update Avibe

```bash
vibe upgrade
# Then restart:
vibe restart --delay-seconds 60
```

### Troubleshooting

If something seems stuck:

```bash
# Check status
vibe status

# Run diagnostics
vibe doctor

# Prefer delayed restart when triggered by an agent
vibe restart --delay-seconds 60
```

## Web UI Controls

The web UI (`http://127.0.0.1:5123`) provides the same controls:

| Button | Equivalent CLI | OpenCode Behavior |
|--------|---------------|-------------------|
| **Start** | `vibe start` | Starts on demand |
| **Restart** | `vibe restart` | Terminated |
| **Stop** | `vibe stop` | Terminated |

## File Locations

| Path | Description |
|------|-------------|
| `~/.vibe_remote/config/config.json` | Main configuration |
| `~/.vibe_remote/state/vibe.sqlite` | Internal database managed by Avibe; stores settings, sessions, scheduled tasks, watches, and background run records |
| `~/.vibe_remote/state/discovered_chats.json` | Discovered IM chats/channels surfaced by platform adapters |
| `~/.vibe_remote/state/settings.json` | Legacy JSON snapshot of channel routing settings |
| `~/.vibe_remote/state/scheduled_tasks.json` | Legacy scheduled task definitions imported into SQLite on startup |
| `~/.vibe_remote/state/watches.json` | Legacy managed watch definitions imported into SQLite on startup |
| `~/.vibe_remote/state/task_requests/` | Legacy queued task/hook requests imported into SQLite on startup |
| `~/.vibe_remote/state/user_preferences.md` | Shared long-term user preference notes |
| `~/.vibe_remote/state/backups/` | Automatic state backups taken before migrations |
| `~/.vibe_remote/runtime/remote-access-cloudflared.pid` | cloudflared tunnel PID for Avibe Cloud remote access |
| `~/.vibe_remote/screenshots/` | Default output directory for `vibe screenshot` |
| `~/.vibe_remote/logs/vibe_remote.log` | Application logs |
| `~/.vibe_remote/logs/opencode_server.json` | OpenCode server PID file |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENCODE_PORT` | Override OpenCode server port (default: 4096) |

## See Also

- [Slack Setup Guide](SLACK_SETUP.md)
- [Telegram Setup Guide](TELEGRAM_SETUP.md)
- [Codex Setup Guide](CODEX_SETUP.md)
