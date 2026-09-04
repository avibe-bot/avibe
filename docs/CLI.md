# Avibe CLI Reference

## Quick Start

```bash
vibe              # Alias for vibe start
vibe start        # Start Avibe if needed (opens web UI)
vibe status       # Check service status
vibe memory status # Read local Memory status from the running controller
vibe restart      # Restart all services (use --delay-seconds when agent-triggered)
vibe remote       # Guided Avibe Cloud remote-access setup
vibe screenshot   # Capture a local desktop screenshot
vibe stop         # Stop all services
```

## Commands

### Bounded list output

Agent-facing collection commands share one pagination contract: `vibe agent list`,
`vibe agent models`, `vibe runs list`, `vibe session list`, Vault list/find/tags,
Show Page list/marks, `vibe data query`, `vibe task list`, and `vibe watch list`.
They return 20 rows by default, accept `--page` and `--limit`, cap a page at 100,
and never provide an unpaginated `--all` bypass. Follow
`pagination.next_command` for the next page and use the corresponding `show` or
`get` command for full record detail.

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

**Known limitation — Memory Settings after a partial restart.** The Web UI and
the service prove local Memory reads to each other with a secret minted once per
launch. It reaches each child over stdin and is never written to disk, so
`vibe start` can only align the processes it starts itself. When the service is
already running and only the Web UI starts fresh, the pair holds no shared
proof and the Memory Settings page reports Memory as unavailable until both are
restarted together; the CLI prints that recovery step — run `vibe stop`, then
`vibe`. The reverse case needs no action: a freshly started service restarts a
surviving Web UI so the new pair shares one secret. `vibe memory ...` uses a
separate session-scoped grant and is unaffected.

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

### `vibe skill`

Avibe gives Claude, Codex, and OpenCode one managed Skill catalog and suppresses
their native Skill catalogs on Avibe-dispatched Turns.

```bash
vibe skill list [--page N]
vibe skill load -- <name>
```

`list` prints the currently available names and descriptions in stable order,
25 per page. Page 1 is also included in the Agent's system prompt; use the
next-page command shown in the output when more Skills are available. `load`
prints only the selected Skill body inside a `skill_content` element. Its
`directory` attribute is an absolute path so the Agent can read references and
run scripts stored beside `SKILL.md`.

Avibe discovers existing Skills without moving them:

- project Skills under `.agents/skills`, `.codex/skills`, `.claude/skills`, or
  `.opencode/skills`, from the working directory up to the Session's bound Avibe
  project base. This boundary can sit above a nested Git checkout. Standalone
  commands without a bound project use the first Git root instead;
- global Skills under `~/.agents/skills`, the configured Codex and Claude Skill
  directories, the OpenCode directory under `XDG_CONFIG_HOME`, and enabled
  Claude plugin Skill directories; and
- Avibe built-ins plus Codex's bundled system Skills.

Built-ins win name conflicts, then project Skills, then global Skills. Within a
project the nearer directory wins; at the same depth the order is `.agents`,
`.codex`, `.claude`, then `.opencode`. User Skills win over Codex bundled
defaults; enabled Claude plugin Skills follow the four static user directories
but also win over those defaults. New global installs should use
`~/.agents/skills/<name>`; project installs should use
`<project>/.agents/skills/<name>`.

Every command resolves from disk, and every new Avibe-dispatched Turn rebuilds
the Catalog. Adding, editing, or deleting a Skill is therefore visible in an
existing Session without restarting Avibe or creating a new Session. Existing
conversation history is not rewritten.

### `vibe memory`

Read scoped local Memory or submit context for best-effort, process-local capture — facts the user explicitly asked to remember, and conclusions the Agent distills on its own from the conversation and from work on this machine, including lasting environment or account facts it meets in files or tool output — through the existing mode-0600 controller socket. Acceptance does not guarantee provider delivery or persistence. This command does not start a service and has no clear, configuration, export, or delete subcommands.

`status` works from a normal terminal. `profile`, `list`, `search`, and `remember`
require an eligible Agent shell where Avibe has injected the current Session
context; running them from a normal terminal returns `memory_access_denied`.

```bash
vibe memory status [--json]
vibe memory profile [--json]
vibe memory list [--project <slug>] [--page N] [--limit 1..100] [--json]
vibe memory search <query> [--project <slug>] [--mode {hybrid|keyword|vector|agentic}] [--limit 1..100] [--json]
vibe memory remember <text> [--project <slug>] [--json]
```

List returns valid processed episodes newest first. It uses EverOS's exact
1-based page semantics, defaults to 20 episodes per page, and exposes each
episode's opaque entry id in JSON. The Agent CLI accepts `default` or one
catalogued named project; `--project all` is reserved for the Settings UI.
Listing is an explicit inspection command and is not added to the injected
Personal Memory prompt.

Search defaults to `--mode hybrid` with `--limit 8`. Use `keyword` for exact
terms, `vector` for semantic matches, and reserve `agentic` for complex,
multi-hop recall. Agentic searches are bounded to 30 seconds and require the
configured LLM, embedding, and rerank capabilities. They fail closed when any
required capability is unavailable, and `--project all --mode agentic` is not
supported.

EverOS returns an empty `atomic_facts` list for agentic episode results. When
EverOS receives unlimited `top_k`, agent case and skill results are capped at
10; the Avibe CLI always sends its explicit bounded `--limit` value.

### `vibe doctor`

Run diagnostic checks on your configuration.

```bash
vibe doctor
```

Run safe first-phase repairs explicitly:

```bash
vibe doctor repair --dry-run
vibe doctor repair home-migration --yes
vibe doctor repair duplicate-service-processes --yes
vibe doctor repair stale-install-runtime --yes
vibe doctor repair stale-restart-state --yes
vibe doctor repair askill --yes
vibe doctor repair avault --yes
vibe doctor repair git-runtime --yes
vibe doctor repair show-runtime --yes
vibe doctor repair tmux --yes
```

**Checks:**
- Configuration file validity
- Slack token configuration
- Agent CLI availability (Claude Code, OpenCode, Codex)
- Runtime home migration state
- Runtime process, install, and restart metadata state
- askill, avault, Git Runtime, Show Runtime, tmux, and Node.js readiness through one dependency diagnostic group
- `vibe doctor --deep` also probes missing dependencies without downloading their bodies
- managed downloads retry transient HTTP, DNS, timeout, and connection failures with bounded backoff

### `vibe remote`

Start the guided Avibe Cloud remote-access setup.

```bash
vibe remote
```

The remote Workbench uses the same Instance role and Project/Agent/Show Page ACLs
as the local UI. Viewers can read permitted resources, Editors can use runtime
surfaces where their Project and Agent access allows it, and Owners can manage
the Instance. Connection, Origin/CSRF, approval, and path-safety checks still
apply independently.

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
vibe session list                       # active sessions, 20 per page by default, newest activity first
vibe session list --type slack          # filter by platform (avibe = Web/Workbench)
vibe session list --page 2 --limit 50   # request page 2 with 50 rows (maximum 100)
vibe session get sesk8m4q2p7x           # full detail for one session
vibe session get                        # inside an Avibe Agent shell, show the caller Session
vibe session update sesk8m4q2p7x --title 'Release review'   # pass "" to clear the title
vibe session update --title 'Release review'                 # inside an Avibe Agent shell
```

`--type` accepts a platform id: `avibe` (Web/Workbench), `slack`, `discord`,
`telegram`, `lark`, `wechat`. For richer filtering — by agent, time range, message
content, or cross-table joins — `list` and `get` point you to `vibe data query`.
When `get` or `update` runs inside an Avibe-injected Agent shell, the session id
may be omitted and defaults to the caller Session from `AVIBE_SESSION_ID`.

### `vibe runs`

List and inspect Agent run records.

```bash
vibe runs list --session-id sesk8m4q2p7x --brief
vibe runs show run_abc123
vibe runs show                         # inside an Avibe Agent shell, show the caller Run
```

`vibe runs list` keeps its global listing behavior unless a filter such as
`--session-id` is provided. `vibe runs show` can omit the run id inside an
Avibe-injected Agent run and defaults to `AVIBE_RUN_ID`.

### `vibe task`

Create, inspect, update, run, pause, resume, or remove scheduled tasks.

```bash
vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
vibe task add --cron '0 * * * *' --message 'Share the hourly summary.'   # inside an Avibe Agent shell
vibe task add --name nightly-sync --cron '0 3 * * *' --shell './scripts/sync.sh'   # command task, no Agent turn
vibe task list
vibe task update <task-id> --cron '*/30 * * * *'
vibe task run <task-id>
vibe task remove <task-id>
```

Use `vibe task add --help` and `vibe task update --help` for the full command surface, including:

- `--session-id` for Agent Session continuity
- `--create-session`, `--create-session-per-run`, `--same-scope`, and `--scope-id` for Session placement
- `--cron` and `--at` scheduling
- `--name`, `--timezone`, and message file support
- `--shell` / trailing `-- <argv>` for a scheduled command with no Agent turn,
  with `--on-failure {none,agent}`, a per-run `--timeout` (default 21600
  seconds, 0 = none), and `--cwd` for the directory the command runs in

When `vibe task add` runs inside an Avibe-injected Agent shell, `--session-id`
may be omitted. Avibe defaults the task target to the caller Session from
`AVIBE_SESSION_ID` and reports that default in the command output. Explicit
`--session-id`, session creation flags, and delivery flags still win.

A pure command task (`--on-failure none`, the default) skips that caller-session
default and takes no session, scope, or agent flags. Successful runs are silent;
a failed run records a durable failure notice naming the command and its exit
code. With `--on-failure agent --message '<instructions>'` the failure instead
starts one Agent turn carrying the failure report, and that turn replaces the
notice for the run. `vibe task update` can change a command task's `--shell`,
argv, `--timeout`, or `--cwd`, but switching a task between message and command
form, or changing `--on-failure`, is rejected — remove the task and recreate it.

`--cwd` is where the command runs. What else it touches depends on whether the
definition also *creates* a Session:

- Bound to an existing Session (`--session-id`, or the caller-session default),
  or to a reusable one already reserved: the flag is the command's alone. The
  escalation Session keeps its own working directory, which is the case the flag
  was added for — a command task binds to a Session so `--on-failure agent` has
  somewhere to land, not to say where it runs.
- Creating one (`--create-session`, `--create-session-per-run`): the flag places
  that Session too, so the escalation turn runs where the command does. Pass a
  scope instead (`--same-scope` / `--scope-id`) and omit `--cwd` if you want the
  Session to inherit its directory.

Without the flag, a Session-bound command follows that Session's directory —
read live at fire time, so `/setcwd` on that conversation relocates the job —
and every other command records the directory you ran `vibe task add` from. For
a message task `--cwd` still places the Session it creates, and is still refused
for one that already exists.

`--session-key` remains accepted for older scripts, but new tasks should use
the Agent Session ID shown in the active Avibe prompt.

### `vibe agent run`

Run an Agent directly. Runs are async by default and do not store a scheduled
task definition. Use `--sync` only when the terminal should wait for completion.

```bash
vibe agent run --no-callback --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --sync --agent release-reviewer --message 'Review the latest deployment result and print it here.'
vibe agent run --no-callback --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --session-id sesk8m4q2p7x --send-now --message 'Apply this correction in the current turn.'
vibe agent run --no-callback --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --session-id sesworker123 --callback-session-id sescaller456 --message 'Run the delegated investigation.'
vibe agent run --no-callback --create-session --scope-id slack::channel::C999 --agent release-reviewer --message 'Post the deployment summary.'
```

With an existing `--session-id`, the default admission is P1: Avibe steers the
new Run into an active native Turn, starts it immediately when idle, or moves the
same Delivery to the durable P3 queue after a definitive refusal/not-active
receipt. It does not interrupt the active Turn.

`--send-now` is valid only with an existing `--session-id` and explicitly selects
the normal content-bearing P1 behavior: the new message steers an active native
Turn, starts immediately when idle, and falls back to P3 after a definitive
refusal. It never promotes an older queued message. `vibe session send-now` is
the content-free P1 operation: it promotes the exact existing FIFO head without
adding a message. A stale head is refused rather than replaced by the next queued
item, and neither command calls Stop.

Use `--fork-session <session-id>` when a new Agent Session should branch from
an existing Session's native backend context instead of starting blank. The new
Session keeps the source backend. `--agent`, `--model`, and
`--reasoning-effort` can override the forked Session only when the Agent backend
stays the same; a cross-backend fork is rejected. Do not combine
`--fork-session` with `--session-id` or `--create-session`.

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

vibe watch list
vibe watch show <watch-id>
vibe watch update <watch-id> --name 'Watch deployment' --timeout 1200
vibe watch pause <watch-id>
vibe watch resume <watch-id>
vibe watch remove <watch-id>
```

`vibe task list` and `vibe watch list` return 20 definitions per page and
include `pagination.next_command` when more rows exist. Successful one-shot
definitions are hidden by default. Add `--include-finished` to page through
history. List output is always bounded; there is no unpaginated `--all` mode.
Task and watch commands use `definition` for one record and `definitions` for
lists; they do not duplicate those records under command-specific aliases.
Both list and show read the same Harness projection as the Workbench:
`lifecycle_state`, `lifecycle_detail`, `next_run_at`, `waiting_since`, and
`running_since`; watch rows also include `process_alive`. For watches,
`process_alive: null` means no waiter runtime has ever been observed, while
`false` means an observed waiter has exited. The older `state` and task
`last_status` fields remain compatibility-only display fields and do not define
the lifecycle.

The waiter command is passed positionally after `--` (or as a single shell
string via `--shell`). Use `vibe watch add --help` for the full surface,
including `--timeout` (per-cycle timeout in seconds), `--lifetime-timeout`
(total wall-clock limit), `--forever`, `--retry-exit-code`, `--retry-delay`,
`--name`, and session creation flags. Watches share `--session-id`,
`--create-session`, `--create-session-per-run`, `--same-scope`, and `--scope-id`
semantics with `vibe task`; direct `vibe agent run` uses `--create-session`
for one-shot session creation. `vibe watch remove` hides the watch from management
views while preserving existing run history in SQLite. Prefer `vibe watch`
over ad-hoc `nohup` jobs when the
user wants a managed background task with a guaranteed follow-up message.
`--timeout` defaults to 21600 seconds; an explicit `--timeout 0` disables the
per-cycle timeout, while any positive value is persisted unchanged.

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

The Model Hub engine process is named `cli-proxy-api` (hyphenated); `pgrep cliproxyapi` therefore always returns no match.

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
