# Avibe Command Reference

This document is the exhaustive command reference for Avibe.

It covers:

- in-chat commands users send to the bot from Slack, Discord, Telegram, WeChat, and Lark
- command aliases and parser normalization rules
- platform differences
- permission requirements
- host-side CLI commands exposed by the `vibe` executable

For installation, setup, and operations background, also see:

- [CLI Reference](./CLI.md)
- [Slack Setup Guide](./SLACK_SETUP.md)
- [Telegram Setup Guide](./TELEGRAM_SETUP.md)

## 1. Command Surface Overview

Avibe exposes two command families:

1. In-chat commands
   - Sent from an IM platform to the bot.
   - Examples: `/start`, `/resume`, `/setcwd ~/repo`, `/setup`.
2. Host CLI commands
   - Run on the machine where Avibe is installed.
   - Examples: `vibe`, `vibe status`, `vibe task add ...`.

The two families solve different problems:

- in-chat commands control conversations, working directories, session resume, DM binding, and backend auth repair
- host CLI commands control the local service process, diagnostics, upgrades, and Agent Harness automation

## 2. In-Chat Commands

### 2.1 Supported in-chat commands

These commands are registered by the controller today:

| Command | Purpose |
| --- | --- |
| `/start` | Show the welcome panel / control entry |
| `/new` | Start a fresh session |
| `/clear` | Alias of `/new` |
| `/cwd` | Show current working directory |
| `/setcwd <path>` | Set working directory |
| `/set_cwd <path>` | Internal-style alias that also works |
| `/resume` | Resume a recent session |
| `/setup` | Repair backend login/auth |
| `/settings` | Open settings UI |
| `/stop` | Interrupt the active backend execution |
| `/bind <code>` | Bind a DM user to this Avibe instance |
| `bind <code>` | Plain-text DM alias for unbound users only |

### 2.2 Permission model

Authorization is centralized in `core/auth.py`.

#### Available to any authorized channel user

- `/start`
- `/new`
- `/clear`
- `/cwd`
- `/resume`
- `/stop`

#### Admin-only commands

- `/setcwd <path>`
- `/set_cwd <path>`
- `/settings`
- `/setup`

Notes:

- `/setup` button callbacks such as `Reset OAuth` are also admin-protected.
- If a workspace has at least one admin configured, non-admins cannot run admin-only commands.

#### DM binding exception

- `/bind <code>` is allowed for unbound DM users.
- Plain `bind <code>` is also allowed for unbound DM users on platforms where plain-text bind is enabled as a workaround.

### 2.3 Platform behavior

#### Slack

- Native Slack slash commands are exposed for `/start` and `/stop` after the Slack App commands are declared.
- Other commands are typically sent as normal bot-directed messages, for example:
  - `@Avibe /resume`
  - `@Avibe /setcwd ~/work/repo`
- In DM, plain `bind <code>` is accepted for unbound users.

#### Discord

- Commands are parsed from normal messages that start with `/`.
- `/resume` opens a Discord-native resume picker flow when the platform interaction context is available.

#### Telegram

- Commands are parsed from normal messages that start with `/`.
- `/resume` and `/settings` prefer inline button flows in the current chat.

#### Lark / Feishu

- Commands are parsed from normal messages that start with `/`.
- `/resume` and `/settings` prefer native cards/modals when available.

#### WeChat

- Commands are parsed from normal text messages that start with `/`.
- `/resume` uses a text-first flow instead of modals.
- `/resume 1`, `/resume more`, `/resume latest ...`, and manual resume syntax are especially relevant on WeChat.

### 2.4 Parser normalization and aliases

The shared parser in `modules/im/base.py` applies these rules:

- `/setcwd /tmp/work` is normalized to internal action `set_cwd`
- `/set_cwd /tmp/work` also works because `set_cwd` is a registered command name
- `bind abc123` is accepted only when plain bind is allowed for that DM user

Backend aliases used by `/resume` and `/setup`:

| Alias | Backend |
| --- | --- |
| `oc` | `opencode` |
| `open-code` | `opencode` |
| `cc` | `claude` |
| `claude-code` | `claude` |
| `cx` | `codex` |

## 3. In-Chat Command Reference

### `/start`

Show the welcome message and the main control entry for the current channel or DM scope.

#### Syntax

```text
/start
```

#### What it does

- shows the current platform
- shows the currently resolved backend for the scope
- shows the current channel when relevant
- lists the main text commands
- opens an interactive menu on platforms that support buttons

#### Typical usage

```text
@Avibe /start
```

#### Notes

- This is the safest discovery command when a user is unsure what is currently configured.
- On some platforms the response appears in-channel rather than inside the thread.

### `/new`

Reset the current session state so the next user message starts a fresh conversation.

#### Syntax

```text
/new
```

#### What it does

- clears active session state for the current scope
- does not delete your repository
- does not change routing or working directory

#### Typical usage

```text
/new
```

### `/clear`

Alias of `/new`.

#### Syntax

```text
/clear
```

#### What it does

- dispatches to the same handler as `/new`

#### Recommendation

- Prefer `/new` in user-facing docs and examples.
- Keep `/clear` in mind for compatibility or old habits.

### `/cwd`

Show the working directory currently associated with the channel or DM scope.

#### Syntax

```text
/cwd
```

#### What it does

- prints the absolute working directory
- reports whether the directory exists
- reminds the user that this is where the backend executes commands

#### Typical usage

```text
/cwd
```

#### Example scenario

- Before asking the agent to edit code, confirm that the current scope points at the correct repository.

### `/setcwd <path>`

Set the working directory for the current channel or DM scope.

#### Syntax

```text
/setcwd <path>
```

Also accepted:

```text
/set_cwd <path>
```

#### What it does

- expands `~`
- converts the path to an absolute path
- creates the directory if it does not exist yet
- saves the custom working directory to the current settings scope

#### Examples

```text
/setcwd ~/projects/myapp
/setcwd /srv/repos/api
/set_cwd ../another-repo
```

#### Permission

- admin-only

#### Notes

- The scope is the current channel for channel chats.
- The scope is the current user for DMs.

### `/resume`

Resume a recent native agent session from the current working directory.

#### Core syntax

```text
/resume
```

#### Text-mode subcommands

```text
/resume 1
/resume more
/resume latest
/resume latest oc
/resume latest cc
/resume latest cx
/resume <backend> <session_id>
```

#### Backend names accepted in text-mode resume

- `oc`
- `opencode`
- `open-code`
- `cc`
- `claude`
- `claude-code`
- `cx`
- `codex`

#### What it does by platform

##### Slack

- `/resume` opens the resume picker when invoked from an interaction-capable context.
- If no modal trigger is available, the bot sends guidance telling the user to open the picker from the menu.

##### Discord

- `/resume` opens a native resume picker flow when the interaction context exists.

##### Telegram

- `/resume` opens an inline-button picker in the current chat.

##### Lark

- `/resume` prefers a native card/modal flow.

##### WeChat

- `/resume` is text-driven.
- `/resume 1` restores item 1 from the current shown list.
- `/resume more` paginates.
- `/resume latest [backend]` restores the newest session.
- `/resume <backend> <session_id>` restores a session manually.

#### Typical usage

```text
/resume
```

Then, on WeChat text flow:

```text
/resume 1
/resume latest cc
/resume codex 123e4567-thread-id
```

#### Notes

- Resume only looks at sessions under the current working directory.
- If the working directory changed since the last list snapshot, selection-by-number expires.

### `/setup`

Repair backend login or provider auth through the IM flow.

#### Syntax

```text
/setup
/setup claude
/setup codex
/setup opencode
/setup vibey
/setup cc
/setup cx
/setup oc
/setup code <value>
/setup code <backend> <value>
```

#### What it does

- resolves the backend for the current scope, unless an explicit backend is given
- for Claude Code, Codex and OpenCode: starts a backend-specific auth recovery flow, sends browser links, device
  codes, or follow-up prompts into chat, then waits for completion and verifies login status
- for Vibey: starts no flow, because Vibey signs in to nothing; it replies where Vibey's models come from (see below)

#### Backend-specific behavior

##### Claude

- starts the Claude login flow
- sends the browser authorization URL into chat
- if Claude later asks for a pasted code, the user submits:

```text
/setup code <value>
```

##### Codex

- starts device auth
- sends a browser URL plus one-time code into chat
- the bot waits for completion and verifies `codex login status`

##### OpenCode

- infers the provider from current OpenCode routing/model when possible
- `openai` uses headless device-style auth
- other common providers such as `opencode` or `anthropic` use key-entry flows
- when OpenCode asks for a key, the user submits:

```text
/setup code <value>
```

##### Vibey

- Vibey gets every model through Model Hub, so there is nothing to sign in to
- replies that model sources are added and managed in Models, ending with where to find Models: the public
  `/settings/models` URL when Avibe Cloud is paired, otherwise the Avibe Workbench
- when Model Hub leaves Vibey no model right now, replies with that cause instead: the gateway is off, or no model
  source exists yet (both end with the same pointer to Models), or Model Hub is disabled on the instance (no pointer,
  since Models is unavailable)

#### Permission

- admin-only

#### Notes

- `/setup code <backend> <value>` is useful when multiple setup flows are open and the backend must be explicit.
- Only the user who started the setup flow can submit its follow-up code or key.

### `/settings`

Open the settings UI for the current scope.

#### Syntax

```text
/settings
```

#### What it does

- opens or routes to the settings menu for the current platform
- allows changing settings such as routing and other scope-level controls through UI flows

#### Permission

- admin-only

#### Notes

- This command is registered even though the exact UI differs by platform.
- Prefer it when users want a guided configuration flow instead of raw commands.

### `/stop`

Interrupt the active backend execution for the current scope.

#### Syntax

```text
/stop
```

#### What it does

- builds a stop request for the currently resolved backend
- asks the backend adapter to interrupt the active task
- if there is no active session, returns an informational response

#### Typical usage

```text
/stop
```

#### Notes

- In threaded platforms, a plain `stop` or `/stop` in-thread may also be recognized by the message path.
- This command does not change routing or clear historical state by itself.

### `/bind <code>`

Bind a DM user to this Avibe instance using a bind code generated from the UI or admin workflow.

#### Syntax

```text
/bind <code>
bind <code>
```

#### What it does

- validates the bind code
- records the DM user as bound
- stores the DM chat ID
- may grant admin on the initial bootstrap bind, depending on the bind-code workflow

#### Permission and context

- DM-only
- allowed even before the user is bound

#### Examples

```text
/bind vr-a3x9k2
bind vr-a3x9k2
```

#### Notes

- Plain `bind <code>` is the compatibility path for platforms or contexts where leading `/` is awkward.
- If the user is already bound, the command returns an already-bound response instead of rebinding.

## 4. What Is Not a Command

These are important user-facing controls, but they are not text commands:

- `AgentName: your message`
  - Example: `Plan: Design a new caching layer`
  - This is the subagent prefix flow, not a command.
- `/start` menu buttons
  - Examples: `Settings`, `Resume Session`, `Change Work Dir`
  - These are button callbacks or modal flows, not slash commands.

## 5. Host CLI Commands

The `vibe` executable controls the local service and async automation features.

All Agent-facing collection commands use `--page` / `--limit` and default to 20
compact rows. A page holds at most 100 rows; a larger `--limit` reads 100 and
`pagination.limit` reports the limit used. They never expose an unpaginated
`--all` mode. Continue with `pagination.next_command`; inspect one record with
its `show` or `get` command. List rows omit null, empty, and zero-valued fields;
`--brief` on `vibe task show`, `vibe watch show`, `vibe runs show`,
`vibe session get`, `vibe agent show`, and `vibe show status` prints exactly the
list row.

Every command follows one grammar: `--session-id <id>` is the only way to name a
Session, and single-Session commands default to the caller Session inside an
Avibe Agent shell. Agent-facing commands print the JSON envelope
(`schema_version`, `ok`, `kind`, fields) and accept `--json` as a no-op; operator
commands print text, and most of them print the envelope with `--json`. CLI text
is English. There are no command or flag aliases. See
[CLI Reference](CLI.md#commands) for the full grammar and output rule.

## 5.1 Top-level CLI commands

| Command | Purpose |
| --- | --- |
| `vibe` | Alias for `vibe start` |
| `vibe start` | Start the service and Web UI if needed; reuse already-running processes; `--no-open-browser` suppresses the browser launch |
| `vibe stop` | Stop the service and UI; also terminates OpenCode server |
| `vibe restart` | Stop then start again |
| `vibe status` | Print runtime status as the JSON envelope |
| `vibe doctor` | Run diagnostics; `vibe doctor repair` applies explicit safe repairs |
| `vibe remote` | Guided Avibe Cloud remote Web UI setup |
| `vibe screenshot` | Capture a local desktop screenshot |
| `vibe computer ...` | Use the Controller-owned Computer Use surface from an active Agent Turn |
| `vibe version` | Show installed version |
| `vibe check-update` | Check for new version |
| `vibe upgrade` | Upgrade to latest version |
| `vibe agent ...` | Manage Avibe Agents and run them directly |
| `vibe runs ...` | Inspect and cancel Agent Run records |
| `vibe task ...` | Manage scheduled tasks |

### `vibe`

```bash
vibe
```

- alias for `vibe start`
- starts the main service if it is not already running
- opens the Web UI
- preserves already-running service, Web UI, and OpenCode processes; use `vibe restart` for an explicit restart
- only warns when it runs an install older than the last activated one, because it is also the systemd and launchd entry

### `vibe start`

```bash
vibe start
vibe start --no-open-browser
vibe start --allow-downgrade
```

- `--no-open-browser` keeps the same start/reuse semantics but suppresses the browser launch. This is the supported contract for desktop shells that own their own WebView.

- starts the main service if it is not already running
- opens the Web UI
- preserves already-running service, Web UI, and OpenCode processes; use `vibe restart` for an explicit restart
- refuses to run an install older than the last activated one, naming both versions and the launcher to run instead; `--allow-downgrade` runs the older build on purpose, and the desktop app only warns

### `vibe stop`

```bash
vibe stop
```

- stops the main service
- stops the UI server
- terminates the OpenCode server too

### `vibe restart`

```bash
vibe restart
```

- stops the main service
- stops the UI server
- terminates the OpenCode server too
- starts the service again after a brief wait

Optional async scheduling:

```bash
vibe restart --delay-seconds 60
```

- prints a confirmation immediately
- exits without waiting
- runs the restart in the background after the specified delay

Recommended usage:

- prefer `vibe restart --delay-seconds 60` when an agent triggers the restart from an active conversation
- use plain `vibe restart` when the user explicitly wants the restart to happen immediately
- like `vibe start`, refuses to restart onto an install older than the last activated one unless `--allow-downgrade` is passed

### `vibe status`

```bash
vibe status
```

- prints runtime status as the JSON envelope (`kind: "service_status"`)
- includes `generation_downgrade` when the running service is older than the last activated install, naming both versions and the launcher that selects the newer one

### `vibe doctor`

```bash
vibe doctor
```

- validates config
- checks platform credentials
- checks backend CLI availability
- checks runtime home migration state
- checks runtime process, install, and restart metadata state
- fails when stable `vibe` launchers select different installed versions, naming each path and version, and warns when the running service is older than the last activated install
- checks askill, avault, Git Runtime, Model Hub engine (CPA), Show Runtime, tmux, and Node.js through one dependency diagnostic group
- `vibe doctor --deep` probes missing dependency endpoints without downloading their bodies
- managed downloads retry transient HTTP, DNS, timeout, and connection failures with bounded backoff

Repair mode is explicit and supports dry-run:

```bash
vibe doctor repair --dry-run
vibe doctor repair home-migration --yes
vibe doctor repair duplicate-service-processes --yes
vibe doctor repair stale-install-runtime --yes
vibe doctor repair stable-launchers --yes
vibe doctor repair stale-restart-state --yes
vibe doctor repair askill --yes
vibe doctor repair avault --yes
vibe doctor repair git-runtime --yes
vibe doctor repair model-hub-engine --yes
vibe doctor repair show-runtime --yes
vibe doctor repair tmux --yes
```

### `vibe remote`

```bash
vibe remote
```

- starts the guided Avibe Cloud remote-access setup
- explains what remote access does before asking for a pairing key
- guides the user to open `https://avibe.bot`, create a remote-access bot, claim a personal domain, and copy the one-time pairing key
- saves remote-access credentials and starts the secure tunnel after pairing

Useful follow-up commands:

```bash
vibe remote status
vibe remote start
vibe remote stop
```

If you already have a pairing key:

```bash
vibe remote pair vrp_abc123
```

### `vibe screenshot`

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

- captures the local desktop as a PNG file
- saves to `~/.vibe_remote/screenshots/` by default
- prints the saved file path by default
- prints the JSON envelope with `--json`
- stays at the host CLI layer; it does not expose in-chat commands, bot buttons, or agent prompt injection

### `vibe computer`

`vibe computer` is the Agent-facing Computer Use surface. It can be run only
from an active Avibe Agent Turn. The CLI supplies only the injected Avibe
Session; the Controller resolves the active Turn server-side.

```bash
vibe computer observe apps
vibe computer observe windows
vibe computer observe window --arg pid=123 --arg window_id=456 --output /tmp/window.png
vibe computer zoom --arg pid=123 --arg window_id=456 --arg x1=0 --arg y1=0 --arg x2=800 --arg y2=600 --output /tmp/zoom.png
vibe computer click --arg pid=123 --arg window_id=456 --arg x=120 --arg y=80
vibe computer type --arg pid=123 --arg window_id=456 --arg text="hello"
vibe computer key --arg key=return --arg pid=123 --arg window_id=456
vibe computer hotkey --arg keys='["cmd","c"]' --arg pid=123 --arg window_id=456
vibe computer scroll --arg direction=down --arg pid=123 --arg window_id=456
vibe computer drag --arg from_x=10 --arg from_y=10 --arg to_x=100 --arg to_y=100 --arg pid=123 --arg window_id=456
vibe computer set-value --arg pid=123 --arg value=enabled --arg element_token=el_1
vibe computer clipboard read
vibe computer clipboard write --arg text="copied text"
vibe computer launch --arg bundle_id=com.example.App
vibe computer force-quit --arg pid=123
```

Each leaf accepts `--args-json`, repeatable `--arg`, and `--json`. `observe
window` and `zoom` accept `--output PATH` for a caller-selected screenshot
file. The CLI has no `--session-id` or Turn-ID option and rejects a manually
supplied driver `session` argument.

The `--json` result exposes the structured driver object at
`result.structuredContent` and retains the legacy text content for compatibility.
`force-quit` sends SIGKILL through the driver and can lose unsaved work.

Computer Use guidance is a built-in managed Skill named `computer-use`, loaded
on demand. It tells Agents to treat screen and clipboard data as untrusted,
observe the exact target before input, avoid focus and pointer movement, and
observe again before retrying an action. Backend-specific Computer Use MCP
configuration is not part of this command surface.

### `vibe version`

```bash
vibe version
```

- prints the installed package version

### `vibe check-update`

```bash
vibe check-update
```

- checks PyPI for a newer version

### `vibe upgrade`

```bash
vibe upgrade
```

- upgrades Avibe using the selected upgrade plan
- moves every stable `vibe` launcher that already selects an Avibe install to the new version together; launchers pointing elsewhere are never changed
- schedules a managed restart after success when Avibe is already running
- keeps Avibe stopped when it was not running before the upgrade

## 5.2 `vibe task`

`vibe task` manages persisted scheduled tasks.

### Supported subcommands

| Subcommand | Purpose |
| --- | --- |
| `vibe task add` | Create a task |
| `vibe task update` | Update a task |
| `vibe task list` | List tasks |
| `vibe task show <task_id>` | Show one task |
| `vibe task pause <task_id>` | Pause a task |
| `vibe task resume <task_id>` | Resume a task |
| `vibe task run <task_id>` | Run immediately |
| `vibe task remove <task_id>` | Delete a task |

### `vibe task add`

```bash
# Agent task, and a command task that escalates to an Agent on failure
vibe task add (--session-id <session_id> | --create-session | --create-session-per-run) (--cron <expr> | --at <timestamp>) (--message <text> | --message-file <file>) [options]
vibe task add (--session-id <session_id> | --create-session | --create-session-per-run) (--cron <expr> | --at <timestamp>) (--shell <cmd> | -- <argv>...) --on-failure agent [options]

# Pure command task: no Agent, and therefore no session target
vibe task add (--cron <expr> | --at <timestamp>) (--shell <cmd> | -- <argv>...) [options]
```

Important options:

- `--name`
- `--session-id`
- `--create-session`
- `--create-session-per-run`
- `--agent`
- `--same-scope`
- `--scope-id`
- `--cron`
- `--at`
- `--message`
- `--message-file`
- `--shell`
- `--on-failure {none,agent}`
- `--timeout <seconds>`
- `--cwd <dir>`
- `--timezone`

A command task (`--shell` or a trailing `-- <argv>`) runs with no Agent turn and
takes no session, scope, or agent flags unless `--on-failure agent` is set.
`--timeout` bounds each command run (default 21600 seconds, `0` = no timeout).
`--cwd` is where the command runs; with `--on-failure agent` bound to an existing
Session it means only that, and the escalation Session keeps its own working
directory. With `--create-session` / `--create-session-per-run` it places that
Session too. Without it, a Session-bound command follows that Session's directory
— read live at fire time — and every other command records the directory you ran
`vibe task add` from. See `docs/CLI.md` for the full rules.

### `vibe task update`

```bash
vibe task update <task_id> [options]
```

Important options:

- `--name`
- `--clear-name`
- `--session-id`
- `--create-session`
- `--create-session-per-run`
- `--agent`
- `--same-scope`
- `--scope-id`
- `--reset-delivery`
- `--cron`
- `--at`
- `--message`
- `--message-file`
- `--shell`
- `--timeout <seconds>`
- `--cwd <dir>`
- `--timezone`

Switching a task between message and command form, or changing `--on-failure`,
is rejected with `task_mode_immutable` — remove the task and recreate it. `--cwd`
repoints a command task's working directory without touching the Session it
escalates to; on a message task it is still refused once the target Session
exists.

### `vibe task list`

```bash
vibe task list [--session-id <id> | --global] [--include-finished] [--q <text>] [--page N] [--limit N]
```

Lists the caller Session's tasks inside an Avibe Agent shell; pass `--session-id`
for another Session or `--global` for every Session (required outside an Agent
shell). Returns compact rows, 20 per page by default and at most 100, enabled
tasks first and each group newest activity first. `--q` searches task text
fields. Use `pagination.next_command` to continue; use `vibe task show <task_id>`
for detail. List and show expose the Workbench lifecycle projection:
`lifecycle_state` is the lifecycle, and `health` appears only when it is not
`healthy`. `vibe watch list` follows the same contract.

### `vibe task show`

```bash
vibe task show <task_id> [--brief]
```

A removed task still reads here, with `lifecycle_state: "removed"` and
`removed_at`. Other `vibe task` commands on it fail with `task_removed`;
`task_not_found` means the id never existed.

### `vibe task pause`

```bash
vibe task pause <task_id>
```

### `vibe task resume`

```bash
vibe task resume <task_id>
```

### `vibe task run`

```bash
vibe task run <task_id>
```

### `vibe task remove`

```bash
vibe task remove <task_id>
```

## 5.3 `vibe agent`

`vibe agent` manages Avibe-owned Agent definitions and runs Agents directly.

Agent definitions are globally named. `name` and `backend` are immutable after
creation; description, model, reasoning effort, and system prompt can be edited.

### Supported subcommands

| Subcommand | Purpose |
| --- | --- |
| `vibe agent list` | List Agents |
| `vibe agent show <name> [--brief]` | Show one Agent |
| `vibe agent models [<name>]` | List available models + reasoning efforts for an Agent or backend |
| `vibe agent create` | Create an Agent |
| `vibe agent update <name>` | Edit mutable Agent fields |
| `vibe agent remove <name>` | Remove an Agent |
| `vibe agent import` | Import global Agents or a portable Agent file |
| `vibe agent run` | Run an Agent once |

### `vibe agent models`

List the models and reasoning-effort levels available to an Agent (by name) or to a
backend directly. Reasoning efforts are returned per model, because they are a property
of the model (Claude's `xhigh` / `max` depend on the model; OpenCode varies by variant).
For OpenCode the list includes custom providers and user-added models.

```
vibe agent models <name>             # an Agent: resolves its backend + current model
vibe agent models --backend claude   # a backend directly, before creating an Agent
vibe agent models --backend opencode --provider deepseek
```

- pass exactly one of `<name>` or `--backend`
- `--provider <id>` filters to one provider and applies to the OpenCode backend only
- `--model <id>` narrows the output to a single model's reasoning efforts

When queried by `<name>`, the result includes `current` — the Agent's configured
`model` / `reasoning_effort` and whether they are still valid. `create` / `update`
accept any value but warn (without rejecting) when it is not in the known set and
point back to this command.

### `vibe agent run`

```bash
vibe agent run (--session-id <session_id> | --fork-session <session_id> | --fork-self)? (--message <text> | --message-file <file>) [options]
```

Important options:

- `--agent`
- `--session-id`
- `--queue`
- `--fork-session`
- `--fork-self`
- `--same-scope`
- `--scope-id`
- `--visible`
- `--model`
- `--reasoning-effort`
- `--close-after`
- `--sync`
- `--message`
- `--message-file`

Without `--session-id` or a fork, the run creates a new Session for `--agent`.
It is background by default, which suits sub-agent style calls; `--visible`
makes it user-facing, and `--same-scope` or `--scope-id` places it.

Runs are asynchronous by default: the command queues the run, prints one `run`
object with its `id` and `session_id`, and uses the callback policy to deliver
the final result later. Use `--sync` only when the terminal should wait for completion.

`--close-after` applies to a new or forked Session. It releases that Agent
runtime after the Run settles while keeping the Session, transcript, and Run
record. It cannot target an existing `--session-id`.

With an existing `--session-id`, the default is P1: steer the new Run into an
active native Turn, start it when idle, or move the same Delivery to P3 after a
definitive refusal/not-active receipt; it targets the new message and never
promotes older queued work. `--queue` selects P3 without attempting a steer.
`vibe session send-now --session-id <id>` is the content-free P1 operation that
promotes the exact existing FIFO head without adding a message. Once the
Controller consumes the request, `vibe runs show <run-id>` shows the persisted
`metadata.delivery_outcome`.

`--fork-session <session_id>` creates a new Agent Session by forking the source
Session's native backend context. It is for alternate investigations or
delegated work that should keep the source context without mutating the source
Session. Forks keep the same backend as the source; `--agent`, `--model`, and
`--reasoning-effort` may override the forked Session only when the backend does
not change. Do not combine `--fork-session` with `--session-id`.

`--model` and `--reasoning-effort` set the model of any new Session the run
creates (a blank new Session or a fork); the Session keeps
it. They are refused with `--session-id` (`model_override_with_existing_session`).
The pair the new Session will run on (each override over the default it would
otherwise keep) is checked: a Vibey model or effort its Model Hub menu does not
offer, or a menu that cannot be loaded, is refused (`session_model_unavailable`);
for claude and codex an unknown value is a warning. `vibe agent models <agent>`
lists valid values.

## 5.4 `vibe runs`

`vibe runs` inspects concrete Agent Run records produced by tasks, watches, or
direct Agent Run calls.

### Supported subcommands

| Subcommand | Purpose |
| --- | --- |
| `vibe runs list` | List the caller Session's recent runs; `--session-id <id>` for another Session, `--global` for every Session |
| `vibe runs show <run_id> [--brief]` | Show one run |
| `vibe runs cancel <run_id>` | Request cancellation; once the run's input reached the live Session turn, this stops that turn like Session Stop, including other runs sharing it |

## 6. Recommended Mental Model

Use the right command family for the job:

- Want to control a conversation from chat:
  - use `/start`, `/resume`, `/setcwd`, `/setup`, `/stop`
- Want to bootstrap or recover DM access:
  - use `/bind <code>` in DM
- Want to control the local daemon or troubleshoot installation:
  - use `vibe`, `vibe status`, `vibe doctor`, `vibe upgrade`
- Want asynchronous automation:
  - use `vibe task ...`, `vibe watch ...`, or `vibe agent run ...`

## 7. Quick Examples

### In chat

```text
@Avibe /start
@Avibe /cwd
@Avibe /setcwd ~/projects/backend
@Avibe /setup
@Avibe /setup codex
@Avibe /setup code 123456
@Avibe /stop
```

### On the host machine

```bash
vibe
vibe status
vibe doctor
vibe task list --global
vibe agent run --no-callback --session-id sesk8m4q2p7x --message 'Share the latest build summary.'
```
