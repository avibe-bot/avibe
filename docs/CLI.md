# Avibe CLI Reference

## Quick Start

```bash
vibe              # Alias for vibe start
vibe start        # Start Avibe if needed (opens web UI)
vibe start --no-open-browser  # Start/reuse Avibe without opening a browser
vibe status       # Check service status
vibe restart      # Restart all services (use --delay-seconds when agent-triggered)
vibe remote       # Guided Avibe Cloud remote-access setup
vibe screenshot   # Capture a local desktop screenshot
vibe stop         # Stop all services
```

## Commands

The `vibe` CLI is mostly called by Agents, so every command follows one grammar
and one output rule.

### Grammar

- **Naming a Session.** `--session-id <id>` is the only way to name a Session.
  A command that acts on one Session (`vibe session get`, `update`, `send-now`,
  `queue list`, `queue remove`, the `vibe show` commands) defaults to the caller
  Session from `AVIBE_SESSION_ID`, as the [Session-owned lists](#session-owned-lists)
  do. Outside an Avibe Agent shell there is no default, and the command fails
  with `missing_caller_session`.
- **Naming anything else.** Agent names, run, task, watch, and Vault secret
  ids, and a queued message id are positional:
  `vibe session queue remove <message-id> --session-id <id>`.
- **Message text.** `--message` or `--message-file`, on every command that sends
  or stores a message.
- **Visibility.** `--visible` makes a new or forked Session from `vibe agent run`
  user-facing (new delegated Sessions are background by default).
  `vibe session update` takes `--visible` or `--hidden`.
- **One spelling.** There are no aliases, hidden compatibility flags, or
  deprecated commands; a removed spelling fails as an unknown argument.

### Output

- **Agent-facing commands** (`agent`, `runs`, `harness`, `session`, `task`,
  `watch`, `show`, `vault`, `data query`, `data skill-usage`, `skill list`, and
  `status`) print one JSON object on stdout:
  `{"schema_version": 1, "ok": true, "kind": "<kind>", ...fields}`. They accept
  `--json`, which changes nothing. `vibe data query --format text` prints rows
  as readable records instead.
- **Failures** of those commands, and every argument error, print the same
  envelope on stderr with `"ok": false`, `"kind": "error"`, a stable `code`, the
  `error` text, and when useful a `hint`, `help_command`, and `details`. The exit
  status is nonzero.
- **Content commands** print the content itself: `vibe skill load` prints Skill
  text, `vibe computer` prints its compact observation format (`--json` returns
  the raw driver payload), `vibe vault run` passes through the output of the
  command it runs, and `vibe debug prompt export` prints its export document.
- **Operator commands** for a person at a terminal print text. `version`,
  `check-update`, `doctor`, `runtime`, `remote` subcommands, `screenshot`, and
  `data retention` print the JSON envelope with `--json`. The service lifecycle
  commands `start`, `stop`, `restart`, `upgrade`, and the guided `vibe remote`
  setup only print progress text and take no `--json`.
- **Language.** CLI help, errors, hints, and notices are English, whatever the
  configured UI language. IM, the Web UI, and notifications follow the
  configured language; the Web doctor panel shows the same diagnostics as
  `vibe doctor` in that language.

### Bounded list output

Agent-facing collection commands share one pagination contract: `vibe agent list`,
`vibe agent models`, `vibe runs list`, `vibe session list`,
`vibe session queue list`, Vault list/find/tags, Show Page list/marks,
`vibe data query`, `vibe task list`, and `vibe watch list`. They return 20 rows
by default and accept `--page` and `--limit`. A page holds at most 100 rows: a
larger `--limit` reads 100, and `pagination.limit` reports the limit used. A
`--limit` or `--page` below 1 is an `invalid_pagination` error. There is no
unpaginated `--all` bypass. Follow `pagination.next_command` for the next page
and use the corresponding `show` or `get` command for full record detail.
`vibe skill list` pages the same Skill catalog the system prompt shows, so it
takes `--page` only.

List rows carry only what is needed to decide the next step and omit null,
empty, and zero-valued fields, so an absent field means unset. A Show Page row
is its `session_id`, `visibility`, `active_url` (the URL to share), `path` (the
workspace to edit), and `updated_at`. An Agent row is its `name`, `backend`,
`model`, `reasoning_effort`, `source`, and `updated_at`; a disabled Agent adds
`enabled: false`. A Vault row, the same from `vibe vault list` and
`vibe vault find`, is the secret's `name`, `kind`, `protection`, `fields` (a
multi-field secret adds `default_field`, the field `NAME` alone selects),
`tags`, `description`, `policy`, a keypair's `signing_addresses`, and
`per_use_sign: true` when `vibe vault sign` can use the key; it never holds a
value. `vibe task show`, `vibe watch show`, `vibe runs show`,
`vibe session get`, `vibe agent show`, and `vibe show status` print the full
record; add `--brief` to print exactly the row the matching list command
prints. A removed Task or Watch shows `lifecycle_state: "removed"` and
`removed_at`, and none of the live fields (`enabled`, health, next fire, or
waiter state) it no longer has.

### Session-owned lists

`vibe harness status`, `vibe runs list`, `vibe task list`, `vibe watch list`,
and `vibe show list` read records that belong to an Agent Session, and share one
scope grammar:

```bash
vibe watch list                          # the caller Session (AVIBE_SESSION_ID)
vibe watch list --session-id sesk8m4q2p7x   # another Session
vibe watch list --global                 # every Session
```

Inside an Avibe Agent shell the caller Session is the default. Outside one there
is no default: pass `--session-id` or `--global`, otherwise the command fails
with `missing_caller_session`. The two flags cannot be combined. Each response
reports the scope it read as `session_id` and `global`, and
`pagination.next_command` repeats the scope flags you passed.

A Session's Runs are the Runs it ran, dispatched from its Agent, or receives a
result from, so delegated work stays visible to the Session that delegated it.
Its Watches are the Watches that follow up in it, its Tasks the Tasks it created
(or that run in it when the creator is unknown or gone), and its Show Page the
page with its id; Watches and Tasks match the Workbench's "this Session" filter.
`vibe harness status` keeps controller faults and any working-directory conflict
that involves the Session.

### Changes run in the service

`vibe agent run`, `vibe task add|update|pause|resume|run|remove`,
`vibe watch add|update|pause|resume|remove`, `vibe session update|send-now`,
`vibe session queue remove`, and `vibe runs cancel` hand the parsed command to
the running Avibe service, which checks the caller and applies it. Reading
commands (`list`, `show`, `get`, `queue list`) still read state directly.

- These commands need a running Avibe service that supports them. They never
  write offline or start the service. `execution: not_executed` means nothing
  was applied: `service_unavailable` when no service answered, and
  `unsupported_operation` from a running service too old for the command. Start
  or update the service explicitly, then try again.
- `execution_unknown` means the change may have been applied, but its answer
  was lost. Inspect the result first (`vibe runs list`, `vibe task show`,
  `vibe watch show`, `vibe session get`); do not retry blindly.
- The CLI reads `--message-file` itself, including stdin and shell
  process-substitution descriptors, before the service checks the request, so
  even a request the service then refuses reads (and may wait for) that input.
  Relative paths, a leading `~`, and default directories (`--cwd`, a command
  task's directory) mean your shell's directory and home.
- `vibe watch add` waits for the new Watch's first cycle as before. That wait is
  bounded by the service, which allows longer when many waiters are recovering,
  so the CLI sets no response deadline of its own for it.

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
- Only warns when it runs an install older than the last activated one: bare `vibe` is also the systemd and launchd entry, so it never refuses to start

### `vibe start`

Start Avibe if needed. Opens the web UI in your browser unless you suppress it.

```bash
vibe start
vibe start --no-open-browser
vibe start --allow-downgrade
```

**Behavior:**
- Reuses the main service and Web UI if they are already running
- Opens the setup wizard at `http://127.0.0.1:5123`
- With `--no-open-browser`, keeps the service/UI lifecycle the same but does not launch a system browser window
- **Preserves running processes** — Use `vibe restart` when you need an explicit restart
- Refuses to run an install older than the last activated one, for example an old `vibe` earlier on `PATH`. The error names both versions and the launcher to run instead; `--allow-downgrade` runs the older build on purpose. The desktop app only warns.
- Started by the desktop app, reuses or replaces only a service and Web UI of that app's own Runtime. If another Avibe is running here, it exits with status 3, says so, and leaves that Avibe running.

### `vibe stop`

Fully stop all Avibe services.

```bash
vibe stop
```

**Behavior:**
- Stops the main service
- Stops the web UI server
- **Terminates OpenCode server** — Use this when you need to restart OpenCode
- Run from inside a desktop app's Runtime (for example, by an agent it started), stops only that Runtime. If the Avibe running here is another one, it stops nothing, explains why, and exits with status 3.

### `vibe restart`

Restart Avibe (main service + Web UI). The OpenCode server is terminated as part of the restart.

```bash
vibe restart
vibe restart --delay-seconds 60
vibe restart --allow-downgrade
```

**Behavior:**
- Stops the main service and Web UI, then re-starts them
- Terminates the OpenCode server
- With `--delay-seconds N`, schedules the restart `N` seconds in the future so an active conversation can receive its reply before the restart lands. Prefer this form when an agent is triggering the restart from inside Slack, Discord, Telegram, Lark/Feishu, or WeChat.
- Refuses, like `vibe start`, to restart onto an install older than the last activated one unless you pass `--allow-downgrade`
- Run from inside a desktop app's Runtime, restarts only that Runtime; otherwise it stops nothing, explains why, and exits with status 3. A restart from the Web UI shows the same refusal instead of restarting.
- On macOS, Avibe runs in the login session of whatever started it, and only your desktop session reaches the Keychain. A restart requested from outside that session, for example over SSH, is therefore carried out by the running Avibe when it is inside the desktop session: the command prints the job ID and says that the running Avibe performs the restart, and Avibe comes back in the desktop session with the environment it already had. The caller's environment is not applied; to change it, restart from a terminal on the Mac's desktop.
- When no Avibe inside the desktop session can do that (none is running, the running one is itself outside the desktop session, or it predates this behavior), the restart still runs from the caller, and a warning says that Avibe will run outside the desktop session: credentials kept in the Keychain (GitHub CLI, Vault) are unavailable until you run `vibe restart` from a terminal on the Mac's desktop. `vibe start` prints the same warning when it starts Avibe from outside the desktop session. A Runtime managed by the desktop app is not affected.

### `vibe status`

Display current service status.

```bash
vibe status
```

**Output:**
```json
{
  "schema_version": 1,
  "ok": true,
  "kind": "service_status",
  "state": "running",
  "running": true,
  "pid": 12345
}
```

When the running service is older than the last activated install, the output
also contains a `generation_downgrade` object with the running `version` and
`generation`, the `activated_version` and `activated_generation`, and the
`launcher` that selects the newer install. Restart through that launcher to run
the new version.

### `vibe skill`

Avibe gives Claude, Codex, and OpenCode one managed Skill catalog and suppresses
their native Skill catalogs on Avibe-dispatched Turns.

```bash
vibe skill list [--page N]
vibe skill load -- <name> [<name> ...]
```

`list` returns the currently available names and descriptions in stable order
as `skills`, with a `pagination` object. Page 1 is also included in the Agent's
system prompt; follow `pagination.next_command` when more Skills are available.
`load` prints each named Skill body inside its own `skill_content` element, in
the order given. Its `directory` attribute is an absolute path so the Agent can
read references and run scripts stored beside `SKILL.md`. An unknown name still
prints the Skills that loaded, then fails with a `skill_not_found` error that
lists the `missing` and `loaded` names.

Avibe discovers existing Skills without moving them:

- project Skills under `.agents/skills`, `.codex/skills`, `.claude/skills`, or
  `.opencode/skills`, from the working directory up to the Session's bound Avibe
  project base. This boundary can sit above a nested Git checkout. Standalone
  commands without a bound project use the first Git root instead;
- global Skills under `~/.agents/skills`, the configured Codex and Claude Skill
  directories, and the OpenCode directory under `XDG_CONFIG_HOME`; and
- Avibe built-ins plus Codex's bundled system Skills.

Built-ins win name conflicts, then project Skills, then global Skills. Within a
project the nearer directory wins; at the same depth the order is `.agents`,
`.codex`, `.claude`, then `.opencode`. User Skills win over Codex bundled
defaults. Claude plugin Skills are not part of the static catalog. New global installs should use
`~/.agents/skills/<name>`; project installs should use
`<project>/.agents/skills/<name>`.

Every command resolves from disk, and every new Avibe-dispatched Turn rebuilds
the Catalog. Adding, editing, or deleting a Skill is therefore visible in an
existing Session without restarting Avibe or creating a new Session. Existing
conversation history is not rewritten.

#### Local Skill statistics and privacy

Skill statistics are **enabled by default**, including after an upgrade when
`runtime.skill_observability_enabled` is absent from the configuration. Avibe
records catalog offers and load outcomes locally for future Harness optimization;
an offer or load does not prove that the Agent followed a Skill or completed a task.

The records include Skill names (including private project/global Skills), source
categories, SHA-256 identity/descriptor/content-version hashes, timestamps,
load results, durations, body byte counts, and available Session/project/Turn,
backend/platform, and Avibe-version associations. Names and Session links remain
identifiable: hashing does not make these statistics anonymous. Unknown execution
attribution stays unknown. Statistics are stored in the local SQLite database,
using `agent_events` and `skill_usage_daily`; there is no statistics dashboard or
cloud upload. This feature adds no stored copies of Skill bodies, descriptions,
prompts, credentials, or plaintext filesystem paths. Normal delivery of a loaded
Skill to your configured Agent/model is unchanged.

To stop new recording, set `runtime.skill_observability_enabled` to JSON `false`
in the active `config/config.json` under the Avibe state root. That root is
`$AVIBE_HOME` when explicitly configured, otherwise `$HOME/.avibe` (or the
legacy `$HOME/.vibe_remote` when the new root is absent). Merge this field into
the existing configuration; do not replace the whole file with this fragment:

```json
{
  "runtime": {
    "skill_observability_enabled": false
  }
}
```

The recorder rereads this setting before writing, so a restart is not required.
Disabling statistics does not disable Skill discovery/loading or delete history.
If you do not want future records or retained history, disable first, then clear.

### `vibe computer`

Computer Use is available to an Avibe Agent only from its active Turn. The CLI
binds the request to the caller's injected Avibe Session; the Controller checks
that Session against its own live active-Turn registry. Turn IDs are not
exported to the subprocess, accepted in the IPC request, or used as driver
authority. Callers cannot provide a Session ID, Turn ID, or driver `session`
value.

```bash
vibe computer observe apps
vibe computer observe windows
vibe computer observe window --arg pid=123 --arg window_id=456 --output /tmp/window.png
vibe computer zoom --arg pid=123 --arg window_id=456 --arg x1=0 --arg y1=0 --arg x2=800 --arg y2=600 --output /tmp/zoom.png
vibe computer activate --arg pid=123 --arg window_id=456
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

Every leaf also accepts `--args-json OBJECT` or `--args-json @FILE`,
repeatable `--arg NAME=VALUE`, and `--json`. `--output PATH` is supported by
`observe window` and `zoom`; it writes or confirms the caller-selected PNG and
prints the resulting path. Human output for observations includes the
accessibility state and element tokens when the driver returns them.

Machine-readable output keeps the structured driver object in
`result.structuredContent` and retains the legacy text content. `force-quit`
uses the driver's SIGKILL operation; unsaved application state can be lost.
`activate` brings one exact window to the front and leaves it there. Input
commands accept `--arg delivery_mode=foreground`, which briefly fronts the
exact window for that action and then restores the previous frontmost window.

The command reaches the running Controller over local control IPC, so the
existing observe-first, exact-window, target-scoped lock, desktop state, and
driver admission checks remain in the same enforcement path. A missing active Turn, `off`, `needs_permission`,
`needs_runtime`, `starting`, `error`, `unavailable`, or `target_busy` result is
reported to the caller; the CLI does not retry it automatically. The driver
session and observation state are keyed by Avibe Session and are retired when
the Session is explicitly ended or archived, rather than at every Turn
boundary.

### `vibe data query`

Run one guarded read-only SQL statement against the local SQLite state database.
Results return 20 rows per page by default and at most 100. It is how an Agent
recalls earlier work in this Session or any other.

```bash
vibe data query --sql "select session_id, title, excerpt(text, 'tax') from digests where text like '%tax%' order by last_active_at desc" --format text
vibe data query --sql "select session_id, at, excerpt(text, 'watch list') from transcript where text like '%watch list%' order by at desc" --format text
vibe data query --sql "select started_at, input, reply from turns where session_id = 'sesk8m4q2p7x' order by started_at" --format text
vibe data query --sql "select session_id, title from sessions where parent_session_id = 'sesk8m4q2p7x'"
vibe data query --sql "select group_concat(name, ', ') as tables from pragma_table_list where schema = 'main'"
vibe data query --sql "select group_concat(name, ', ') as columns from pragma_table_info('run_definitions')"
vibe data query --sql "PRAGMA table_info(run_definitions)" --limit 100   # with types and keys
vibe data query --sql "select id, status, created_at from agent_runs order by created_at desc"
vibe data query --sql-file query.sql --limit 50
```

Four history views give every query a stable way into conversation history; the raw
tables stay queryable. Start from `digests` to recognize a Session, then read
its details in `transcript` or `turns`. Cache diagnostics add two connection-local
views described below; they do not create database tables:

| View | Columns | Meaning |
| --- | --- | --- |
| `digests` | `session_id`, `title`, `agent_name`, `last_active_at`, `parent_session_id`, `digest_at`, `mind`, `memo`, `now`, `text` | One row per Session that has something to say: what it is about and where it stands. For a Vibey Session that has written a checkpoint, the latest checkpoint's memory, written at `digest_at`: `mind` (for whom and why, goals, key decisions, lessons), `memo` (the user's own words it keeps), and `now` (progress and next steps); `text` holds all three. Otherwise `text` is the Session's first input and latest reply. |
| `sessions` | `session_id`, `title`, `agent_name`, `backend`, `model`, `status`, `workdir`, `created_at`, `last_active_at`, `parent_session_id` | One row per Session. `parent_session_id` is the Session whose Agent started it with `vibe agent run`. |
| `transcript` | `session_id`, `at`, `role`, `kind`, `text`, `turn_id`, `message_id` | Every message with text, in delivery time `at`. `role` is `user`, `agent`, or `harness` (input Avibe delivered: another Agent's brief, a callback, a Watch or Task); `kind` is the message type, such as `result` for an Agent's reply. |
| `turns` | `session_id`, `turn_id`, `started_at`, `ended_at`, `outcome`, `input`, `reply` | One row per recorded Agent Turn: the text it was given and its final reply. Older history without Turn records is in `transcript`. |
| `_cache_rows` | Internal source view for cache diagnostics | Response, attempt, and fork-turn rows normalized before `cache_requests`; use `cache_requests` for a stable public shape. |
| `cache_requests` | `request_id`, `session_id`, `turn_id`, `at`, `started_at`, `provider`, `protocol`, `model`, `purpose`, classification/facts, valuation | Per-request Vibey cache facts and a current-list cost valuation. Rows with no `cache` object are explicitly `timestamp_inferred`; `valuation` is JSON and may be a lower bound or have no USD value. |

Two functions help with long text. `excerpt(text, 'term')` returns about 300
characters of `text` around the first case-insensitive match of `term` (pass a
third argument for another width); without a match it returns the opening.
`text REGEXP 'pattern'` matches a Python regular expression, such as
`'(?i)PR ?#?2868'`. Every text value longer than `--max-chars` (default 2000)
is cut and ends `…[+N chars]`; the response says how many values were cut, and
`--max-chars 0` keeps values whole.

`--format text` prints each row as a record for reading long text such as a
transcript or a message body: a `-[ RECORD n ]` line, then one `column: value`
line per column. Values are printed as stored, with real newlines; a multi-line
value starts on the line after its column name, indented two spaces, and SQL
`NULL` prints as `(null)`. When more rows exist, the last line is
`next_command: ...`, which keeps `--format text`. JSON stays the default, and
errors are the JSON error envelope on stderr in both formats.

The connection is read-only: writes, schema changes, `ATTACH`, and transactions
are refused. The schema PRAGMAs `table_list`, `table_info`, `table_xinfo`,
`index_list`, `index_info`, and `foreign_key_list` run as statements or as
`pragma_*` table-valued functions; every other PRAGMA is refused. A
`no such table` or `no such column` error carries a `hint` with the closest
names and the table list, or the referenced tables' columns. Tasks and Watches
are rows of `run_definitions` (`definition_type` is `scheduled` or `watch`).

### `vibe data cache`

Summarize Vibey cache behavior for the last seven days, or an explicit UTC time
range. This command is read-only and requires the Member-level diagnostic access
needed to inspect `agent_events`.

```bash
vibe data cache
vibe data cache --since 30d --until 2026-10-31T00:00:00Z
vibe data cache --session-id sesk8m4q2p7x --model claude-opus-5-5
```

The report groups request facts by served route and includes idle bands,
active/passive/provider-miss/estimated-miss classes, causes, TTL write buckets, prompt freshness
by tier, and lifetime what-if estimates where the route supports an override.
Cost values use the cached local standard price list, including any local alias or
default rate; they are not historical provider bills or subscription charges.
Missing usage, unknown TTLs, inferred legacy timestamps, aliases, default rates,
and unavailable fast-mode information remain labeled in the JSON result. Valuation
also reports the concrete price key, per-token rate multipliers, and whether cache
rates are listed or defaulted, with an explicit flag when the read discount is
unknown. What-if
figures keep active breaks and provider misses as misses, recover only passive
expiry, and reprice all known writes at the candidate lifetime's rate (Claude's
static tools/system prefix stays at one hour). Requests with unknown TTL writes or
an unreported aggregate write count are excluded from what-ifs, not reassigned.

### `vibe data retention`

```bash
vibe data retention --json          # Inspect the trace-retention plan
vibe data retention --run           # Delete eligible traces in bounded batches, online
# From a separate terminal, when you intend to take Avibe offline:
vibe stop
vibe data retention --compact       # Also reclaim disk space with VACUUM
vibe start
```

`--compact` implies `--run` and refuses before retention changes anything if the
service or Web UI is running or starting. It never stops either automatically.
Keep both stopped for the operation: service/UI startup is excluded through the
rewrite, final checkpoint, and database cleanup. Disk-space and checkpoint safety
checks still apply, so compaction can be skipped or deferred. Without `--compact`,
manual and automatic retention remain online; they do not run `VACUUM`.

### `vibe data skill-usage`

Instance Owner-only local maintenance; these commands return JSON diagnostics,
not a Skill popularity ranking. The same owner boundary applies to reading these
statistics through `vibe data query`.

```bash
vibe data skill-usage --json
vibe data skill-usage --clear --yes --json
```

The first command reports `enabled`, raw-event and daily-row counts, retention
windows, the first-observation marker, and `cleared_through`. After opting out,
confirm `enabled` is `false`. The clear command requires both `--clear` and
`--yes`; it deletes only Skill statistics, leaves conversations and other event
types intact, and reports deleted row counts. A clear watermark rejects delayed
pre-clear observations. Clearing does not disable collection: new activity can
immediately create new records while the setting remains enabled.

Raw Skill events have a 90-day retention window; daily statistics retain 365 UTC
dates including today. Daily rows are still Session-associated, not anonymous
global totals. Cleanup runs in bounded background batches while the controller
runs, so expired rows can remain until maintenance catches up. These windows
apply independently of the Skill-collection and tool-trace-retention switches.
Archiving a Session does not delete its statistics; physically purging the
Session removes its associated Skill events and daily rows.

Clear and retention are logical deletion, not secure erasure or guaranteed disk
space reclamation: old data may remain in SQLite WAL/free pages or backups.
Restoring a database backup also restores its historical statistics and clear
watermark; clear again after a restore when needed. No historical conversation
scan or backfill is performed by this feature.

### `vibe doctor`

Run diagnostic checks on your configuration.

```bash
vibe doctor
vibe doctor --json
```

Run safe first-phase repairs explicitly:

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

**Checks:**
- Configuration file validity
- Slack token configuration
- Agent CLI availability (Claude Code, OpenCode, Codex)
- Runtime home migration state
- Runtime process, install, and restart metadata state
- Stable `vibe` launchers that select different installed versions (fails with both paths and versions; `stable-launchers` points them all at the newest one, and changes nothing when two installs tie as the last activation)
- A running service older than the last activated install (warns)
- askill, avault, Git Runtime, Model Hub engine (CPA), Show Runtime, tmux, and Node.js readiness through one dependency diagnostic group
- avault's one-time macOS Keychain authorization (warns; Doctor never authorizes, see `vibe vault key authorize`)
- `vibe doctor --deep` also probes missing dependencies without downloading their bodies
- managed downloads retry transient HTTP, DNS, timeout, and connection failures with bounded backoff

### `vibe vault fetch`

Make a credentialed HTTP request through avault without returning the secret:

```bash
vibe vault fetch --auth GITHUB_API.token --url https://api.github.com/user
```

The CLI is a thin client of the running Avibe service; it opens no SQLite
connection. The calling process reads `--data-file`, validates/writes `--output`,
and prints the upstream body. Non-2xx responses still print their body and exit 1.
Existing host policies, protected approvals, one-shot grants and audits apply.

A missing service or one without the control endpoint returns `service_unavailable`;
a service with that endpoint but without Vault fetch support returns
`unsupported_operation`. Both carry `execution: not_executed`.
Start/update it explicitly; there is no offline
fallback or automatic start. A lost or malformed post-send response is
`execution_unknown`, never a reason to retry automatically. Approval waits use
the existing callback arbitration; an approval-completion response lost before
the next fetch may leave neither an automatic fetch nor a callback. Inspect the
request/upstream state rather than re-arming that continuation.

For Show `api/*.ts` handlers, the `use-show-pages` Skill documents the Node-only
server adapter, which avoids starting a CLI process for each request. Existing
pages are not rewritten automatically.

### `vibe vault key authorize`

Authorize avault, once, to read the Vault key in this Mac's Keychain.

```bash
vibe vault key authorize
```

avault 0.1.8 and later are Developer ID signed, and macOS asks once before a newly
signed avault may read a Vault key that an earlier avault created. Until then every
Vault use fails with `avault_authorization_required`, and the Vault page, Settings →
Dependencies, and `vibe doctor` say so. Authorize with **Authorize avault** on the
Vault page, or run this command in Terminal on the Mac's desktop. Either way the macOS
dialog appears on the Mac running Avibe, not in a browser: enter that Mac's login
password and choose **Always Allow**. Later signed avault updates read the key without
asking again.

- A denied or cancelled dialog is not shown again for about 2 minutes.
- Over SSH no dialog can appear; use the Vault page instead.
- Agents never run it: a Vault command that hits this error tells the Agent to ask you.

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

Use `--json` on these subcommands for the JSON envelope.

### `vibe screenshot`

Capture the local desktop as a PNG file.

```bash
vibe screenshot
vibe screenshot --output /tmp/screen.png
vibe screenshot --json
```

**Behavior:**
- Saves to `~/.vibe_remote/screenshots/` by default
- Prints the saved file path, or the JSON envelope with `--json`
- Stays at the CLI layer only; it does not add IM commands, bot buttons, or agent prompt injection

### `vibe session`

List, inspect, update, and drive Agent sessions. `list`, `get`, and `models`
are read-only; `update` changes the title, visibility, scope, model, reasoning
effort, or speed. Archived sessions are soft-deleted and never surfaced.

```bash
vibe session list                       # active sessions, 20 per page by default, newest activity first
vibe session list --type slack          # filter by platform (avibe = Web/Workbench)
vibe session list --mine                # inside an Agent shell, Sessions exchanged with the caller
vibe session list --project "Release"  # active Sessions in a Project by display name or id
vibe session list --page 2 --limit 50   # request page 2 with 50 rows (at most 100)
vibe session get                        # inside an Avibe Agent shell, the caller Session
vibe session get --session-id sesk8m4q2p7x                   # full detail for another Session
vibe session update --title 'Release review'                 # inside an Avibe Agent shell
vibe session update --title 'Release review' --if-untitled   # keep a title a user or Agent set
vibe session update --session-id sesk8m4q2p7x --title ''     # clear the title
vibe session update --session-id sesk8m4q2p7x --hidden       # background-only; --visible promotes it
vibe session models --session-id sesk8m4q2p7x                # current model, effort, speed, and what it can switch to
vibe session update --session-id sesk8m4q2p7x --model gpt-6.1-sol --reasoning-effort high
vibe session update --session-id sesk8m4q2p7x --speed fast   # or --speed standard
vibe session queue list --session-id sesk8m4q2p7x            # queued messages in FIFO order
vibe session queue remove msg_queued123 --session-id sesk8m4q2p7x
vibe session send-now --session-id sesk8m4q2p7x              # promote the exact queue head
```

Session list rows are lean and include `id`, title/project/agent fields when
present, recent activity, and a one-line `status` when the Session has a
current digest `now` block or latest result reply. Status is collapsed to one
line and capped at 200 characters. `--mine` requires the injected caller
Session and includes Sessions connected by real Agent runs in either direction
(including result callbacks and background workers), while excluding archived
Sessions, runtime-owned system Sessions, and Harness-only callback plumbing.
`--mine` and `--project` are mutually exclusive; either may be combined with
`--type`. Project targets accept a stable id or an exact display name after
normalizing input whitespace; duplicate names resolve by creation order.
Disabled Projects are rejected, but a working directory is not required to list
Sessions in a registered Project.

Every command except `list` acts on one Session: `--session-id`, or the caller
Session inside an Avibe Agent shell (see [Grammar](#grammar)). With `--if-untitled`,
`update --title` writes only when no user or Agent has titled the Session; an
automatic title still counts as untitled. The check and the write are one
transaction, so a concurrent rename is never overwritten, and `updated: false`
reports that the existing title was kept. `--type` accepts
a platform id: `avibe` (Web/Workbench), `slack`, `discord`, `telegram`, `lark`,
`wechat`. For richer filtering — by agent, time range, message content, or
cross-table joins — use `vibe data query`.

#### A Session's model, effort, and speed

`vibe session models` shows what the Web model picker shows for one Session.
`current` holds its model, reasoning effort, and speed (`fast` or `standard`),
where the model and effort come from (`session` when set on it, `agent` when
inherited from its Agent), and `turn_running`. `models` lists what it can
switch to: each model's `reasoning_efforts`, and `fast` (`offered`, or
`unconfirmed` when the last fast response did not run at the fast tier) on a
model with a fast tier. The menu is Model Hub's live answer; a Direct Claude
Code or Codex Agent lists its bundled catalog and offers no fast mode.
`vibe agent models <agent>` lists the same menu for an Agent.
`changeable: false` with a `reason` says the caller may not change it.

`vibe session update --model`, `--reasoning-effort`, and `--speed` make the same
change as the Web picker. The reply adds `applies_from`, `turn_running`, and
any `notes`:

- `applies_from: "next_request"` on a Vibey Session. A running Turn takes the
  change at its next model request, so there is no need to wait for the Turn
  or stop it. The Turn keeps its tool list and system prompt until it ends.
- `applies_from: "next_turn"` on a Claude Code, Codex, or OpenCode Session. A
  running Turn keeps its settings, and so does a message that joins it, so
  send the next message with `vibe agent run --session-id <id> --queue`, or
  stop the Turn with `vibe runs cancel <run-id>` first.

Like the Web picker, a new model whose
efforts do not include the current one clears it (the Agent's default then
applies), and fast stays set across a model without a fast tier, which runs at
the standard tier. The CLI never changes the remembered fast default for new
Web Sessions. Refusals:

| Code | When |
| --- | --- |
| `model_family_unsupported` | A Claude Code Session asked for a non-Claude model, or a Codex Session for a non-GPT one. Start a new Session on an Agent that runs it, or switch the Session's Agent in the Web UI. |
| `session_model_unavailable` | The model is not on a Model Hub or Vibey menu. A Direct Claude Code or Codex catalog can lag a release, so there a same-family model it does not list is a note instead. |
| `session_effort_unavailable` | The model does not offer that effort. |
| `fast_unavailable` | `--speed fast` on a model without a fast tier. |
| `bot_model_is_users` | Another Agent or Bot changing a Bot's Session. A Bot's model is its user's: only the user (a terminal with no calling Agent Session) or that Bot itself changes it. |

### `vibe runs`

List and inspect Agent run records.

```bash
vibe runs list                         # inside an Avibe Agent shell, the caller Session's runs
vibe runs list --global --definition-id <task-or-watch-id>   # every fire, across per-run Sessions
vibe runs show run_abc123
vibe runs show run_abc123 --brief      # status, timing, error, and callback fields only
vibe runs show                         # inside an Avibe Agent shell, show the caller Run
```

`vibe runs list` follows the [Session-owned list](#session-owned-lists) scope
grammar. `vibe runs show` can omit the run id inside an
Avibe-injected Agent run and defaults to `AVIBE_RUN_ID`. It prints the full
record once: status and timing, `error`, the callback fields, the text sent to
the Agent as `message`, the reply as `result_text`, and where the Run ran and
came from (Agent, Session, `source_kind`, `source_session_id`,
`parent_run_id`). Reply text delivered beside the final reply, such as a
message sent mid-turn, is `other_outputs`; delivery bookkeeping, turn and
process ids, and copies of other fields are not printed. `--brief` prints the
same compact row as `vibe runs list`, for status checks.

### `vibe model-requests`

Inspect the model requests Vibey kept because they did not succeed: the exact
bytes sent, with the outcome and the raw response read.

```bash
vibe model-requests list                       # newest first; --session-id and --state narrow it, --page and --limit page it
vibe model-requests show mr_20261031T140102Z_0a1b2c3d4e5f
vibe model-requests export mr_20261031T140102Z_0a1b2c3d4e5f --to ~/private/requests
```

`list` and `show` print metadata only, never a request or response body.
`export` copies one request's files into a new private directory `<dir>/<id>/`.
The files can hold prompts and tool output. See
[Model request artifacts](model-request-artifacts.md) for what is kept, for how
long, and why these are the bytes sent to the local Model Hub rather than the
provider's.

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
`AVIBE_SESSION_ID`; the printed definition's `session_id` shows the resolved
target. Explicit `--session-id` and session creation flags still win. To target
an IM channel or thread that has no Session yet, create one with
`--create-session` (or `--create-session-per-run`) and `--scope-id`.

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

### `vibe agent run`

Run an Agent directly. Runs are async by default and do not store a scheduled
task definition. Use `--sync` only when the terminal should wait for completion.

This command needs the running service; see
[Changes run in the service](#changes-run-in-the-service).

```bash
vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'
vibe agent run --agent release-reviewer --message 'Review the deployment result and report the findings here.'
vibe agent run --sync --agent release-reviewer --message 'Review the latest deployment result and print it here.'
vibe agent run --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
vibe agent run --session-id sesk8m4q2p7x --queue --message 'Do this after the current turn.'
vibe agent run --bot cso --message 'Review the new login flow for security risks.'
vibe agent run --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
vibe agent run --bot cso --no-reply --message 'Record this deployment fact; no response is needed.'
vibe agent run --scope-id slack::channel::C999 --visible --agent release-reviewer --message 'Post the deployment summary.'
vibe agent run --close-after --agent release-reviewer --message 'Run a disposable batch review.'
```

Without `--session-id` or a fork flag, the run creates a new background Session
for `--agent`; `--same-scope` or `--scope-id` places it, and `--visible` makes it
user-facing.

With an existing `--session-id`, the default admission is P1: Avibe steers the
new Run into an active native Turn, starts it immediately when idle, or moves the
same Delivery to the durable P3 queue after a definitive refusal/not-active
receipt. It does not interrupt the active Turn, and it never promotes an older
queued message. `--queue` puts the new Run in the P3 queue behind the active
Turn instead. `vibe session send-now` is the content-free P1 operation: it
promotes the exact existing FIFO head without adding a message. A stale head is
refused rather than replaced by the next queued item, and neither command calls
Stop.

`--bot <handle>` messages a Bot's permanent Session (see `vibe bot`). The run is
queued behind the Bot's earlier messages, as with `--queue`, so a Bot handles
one message at a time. A Bot keeps one conversation on its own model:
`--agent`, `--session-id`, the fork flags, `--model`, `--reasoning-effort`, `--speed`,
the placement flags, `--cwd`, and `--close-after` are refused with `--bot`.
`--agent` keeps its meaning for an Agent that is also a Bot: it starts a new
Session of that Agent, never a message to the Bot.

Use `--fork-session <session-id>` when a new Agent Session should branch from
an existing Session's native backend context instead of starting blank. The new
Session keeps the source backend. `--agent`, `--model`, and
`--reasoning-effort` can override the forked Session only when the Agent backend
stays the same; a cross-backend fork is rejected. Do not combine
`--fork-session` with `--session-id`.

`--model`, `--reasoning-effort`, and `--speed` set the model, effort, and speed
of any new Session the run creates (a blank new Session or a fork), and the
Session keeps them for its later turns. A new Session runs at the standard tier
unless `--speed fast` is given; a fork keeps its source's speed. They are
refused with `--session-id`: switch an existing Session with
`vibe session update` (see [A Session's model, effort, and speed](#a-sessions-model-effort-and-speed)).
The values the new Session will run on (each override over the default it
would otherwise keep) are checked against the same menu `vibe session models`
shows, with the same refusals. `vibe agent models <agent>` lists valid values.

The command prints one `run` object: its `id`, `status`, `run_type`,
`agent_name`, and the resolved `session_id`, `scope_id`, and `visibility`.
`no_reply` is included when set. `--sync` replaces this with the finished run
record; historical callback fields remain readable in stored records.

Successful final callbacks are retired. The target's input tells it how to
report to the sender with `vibe agent run`, except when the sender is a Session
that the receiver itself dispatched. If the target handles the message without
writing back, Avibe sends the sender one read receipt once the target is idle
with no pending work. It includes the last reply with its formatting, up to
30,000 characters, then points to `vibe data query` for the full transcript.
An armed Watch, pending Task, or open delegate defers the receipt; a receipt is
not an explicit progress or completion report. Failed, cancelled, or interrupted
runs still send an immediate notice to their caller Session.

Use `--no-reply` with an existing `--session-id` or `--bot` for informational work:
the receiver sees `[No reply needed.]`, and that message does not open or
re-arm a delegation, but it closes an existing reverse delegation, and emits no
read receipt. It does not disable failure notices. Combining it with
`--sync` or a new/forked Session is a usage error.

`--callback`, `--callback-session-id`, and `--no-callback` are accepted no-ops,
with one deprecation line on stderr and unchanged JSON stdout. Old pending
callbacks are honored once. Internal failure delivery and Vault approval
auto-resume are unchanged. Older same-epoch binaries can ignore `no_reply`;
one extra report hint or receipt during rollback is an accepted compatibility
cost. Use `vibe runs show` to read any Run's stored result.

Use `--close-after` for a new or forked Session when the Agent runtime should be
released after the Run settles while the Session, transcript, and Run record stay
available. It is rejected for `--session-id`, which targets an existing Session
with its own runtime lifecycle.

### `vibe bot`

Manage Bots: standing members with one permanent Session and a directory of
their own. A Bot is an enabled Vibey Agent; its role is the Agent's
description. Bots belong to the instance Owner.

```bash
vibe bot list
vibe bot show main
vibe bot create --name 'Chief Security Officer' --handle cso --role 'Reviews security risks.'
vibe bot set reviewer --name 'Reviewer'
vibe bot update cso --name 'Security' --emoji '🛡️' --hue 210
vibe bot remove cso
```

- `list` prints every Bot in the order the Web shows them: the main Bot first,
  then the Bots you last sent a message, newest first, then those never
  messaged, in creation order. `show` prints one by handle.
- `create` makes a Vibey Agent named after the handle and makes it a Bot.
  `--handle` is lowercase ASCII, 2 to 24 letters, digits, or hyphens, starting
  with a letter and not ending with a hyphen; `main`, `memory`, and `ops` are
  reserved. Without `--handle`
  the handle comes from the name: an ASCII name is normalized, any other name
  is transliterated, and a taken handle gets `-2`. Without `--model` the Bot
  follows the main Bot's model and effort.
- `set` makes an existing enabled Vibey Agent a Bot. Its handle is its Agent
  name; an Agent that was a Bot before gets its old handle, Session, and
  directory back. The built-in `vibey` Agent and the default Agent cannot be
  set; a Bot's Agent cannot become the default Agent either.
- `update` changes the display name or the emoji avatar (`--emoji` with
  `--hue`, or `--clear-avatar`). Change the role, model, or instructions with
  `vibe agent update`.
- `remove` drops the Bot role and keeps the Agent, the Session, and the
  directory. The main Bot cannot be removed.

A user can have at most five Bots besides the main Bot. `create` and `set` are
refused with `bot_limit_reached` while five exist; `remove` one to free a place
(its conversation and memory are kept, and setting it again reconnects them).
An install that already has more keeps them all; only adding waits until fewer
than five remain.

The handle never changes once the Bot exists: the Bot's Session is
`ses_<handle>` and its directory is `~/.avibe/bots/<handle>/`. The main Bot's
Session is `ses_main`. While an Agent is a Bot it stays enabled and cannot be
renamed or deleted. The directory starts as an empty Git repository; Avibe
commits it before each Turn of the Bot's Session (changes by others) and after
it (the Bot's own changes), and never squashes its history.

Each command prints a `bot` object with `handle`, `display_name`, `avatar`,
`role`, `agent_name`, `session_id`, `builtin`, `hide_details`,
`last_user_message_at` (when you last sent the Bot a message, or `null`), and
`directory` (`list` prints `bots`; `remove` prints `bot_removed`).

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

`vibe task list` and `vibe watch list` read the caller Session by default (see
[Session-owned lists](#session-owned-lists)), return 20 definitions per page, and
include `pagination.next_command` when more rows exist. Enabled definitions come
first, each group ordered by most recent activity. `--q <text>` searches the
same text fields as the Workbench search (id, name, Agent, Session, message, and
command) and is carried into `next_command`. Successful one-shot definitions are
hidden by default. Add `--include-finished` to page through history. List output
is always bounded; there is no unpaginated `--all` mode. Task and watch commands
use `definition` for one record and `definitions` for lists; they do not
duplicate those records under command-specific aliases.

Both list and show read the same Harness projection as the Workbench:
`lifecycle_state` is the lifecycle, with `lifecycle_detail` and
`lifecycle_finished_at` for a finished definition and `next_run_at`,
`waiting_since`, or `running_since` while it is in play. A row names an unnamed
definition by `message_preview` or `command_preview` and carries `last_error`
and a non-zero `last_exit_code`. Rows and `show` print `health` (and a watch's
`processing_health`) only when the verdict is not `healthy`, and failure
counters only when non-zero. A watch row adds `process_alive: false`
only when an enabled watch's observed waiter has exited; `vibe watch show`
prints the observed `process_alive` and `runtime`, and omits both when no waiter
has ever been observed. Task and watch add, update, pause, and resume print the
same `definition` as `show`, plus `warnings` when there are any.

`vibe task show` and `vibe watch show` also read a removed definition: the same
projection with `lifecycle_state: "removed"` and `removed_at`. Lists never
include removed definitions. `pause`, `resume`, `update`, `run`, and `remove` on
a removed definition fail with `task_removed` / `watch_removed`, whose
`details.removed_at` says when it was removed; `task_not_found` /
`watch_not_found` means the id never existed.

The waiter command is passed positionally after `--` (or as a single shell
string via `--shell`). Use `vibe watch add --help` for the full surface,
including `--timeout` (per-cycle timeout in seconds), `--lifetime-timeout`
(total wall-clock limit), `--forever`, `--retry-exit-code`, `--retry-delay`,
`--name`, and session creation flags. Watches share `--session-id`,
`--create-session`, `--create-session-per-run`, `--same-scope`, and `--scope-id`
semantics with `vibe task`; `vibe agent run` creates a new Session whenever it
names no `--session-id` and no fork. `vibe watch remove` hides the watch from management
views while preserving existing run history in SQLite. Prefer `vibe watch`
over ad-hoc `nohup` jobs when the
user wants a managed background task with a guaranteed follow-up message.
`--timeout` defaults to 21600 seconds; an explicit `--timeout 0` disables the
per-cycle timeout, while any positive value is persisted unchanged. A waiter exit
listed in `--retry-exit-code` means "still waiting": the watch records it as
`last_exit_code` and keeps `last_error` for real failures.

### `vibe version`

Show the installed version.

```bash
vibe version
vibe version --json
```

### `vibe check-update`

Check if a newer version is available.

```bash
vibe check-update
vibe check-update --json
```

### `vibe upgrade`

Upgrade to the latest version.

```bash
vibe upgrade
```

If Avibe is already running, the command schedules a managed restart so the
service and Web UI switch to the upgraded code. If Avibe is stopped, the command
keeps it stopped and the new version is used on the next start.

Activation moves every stable `vibe` launcher that already selects an Avibe
install to the new version together, so no `PATH` lookup keeps an older build.
Launchers that point anywhere else are never changed. See
[Upgrading](UPGRADING.md#stable-launchers).

When Avibe runs on a uv-managed CPython, the upgrade first moves that Python
minor version to its newest patch release with
`uv python install --upgrade --no-bin`, so CPython and OpenSSL security fixes
arrive with Avibe releases. It never changes the minor version and adds no
`python3.x` command to your `PATH`. As with `uv python upgrade`, other uv
environments that follow this minor version's managed Python move to the new
patch too. uv only knows patches released before its own version; with uv older
than 0.9.10, offline, or when the download fails, the upgrade continues on the
installed patch and logs why. Run `uv self update` to reach newer patches.

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

#### Responses streams and overload recovery

With automatic Model Hub recovery enabled, the first OpenAI Responses stream is
buffered before delivery, including compatible custom Sources. You may therefore
see no incremental text, reasoning, or tool calls until that response ends.
If it ends with the exact `server_error` message
`Our servers are currently overloaded. Please try again later.`, Model Hub
discards that partial response and retries once through your configured route,
within the same recovery window. Only the final attempt reaches the Agent.
Other streamed errors and other protocols do not gain post-output retries.

The retry itself streams normally and is not replayed again after output.
Both upstream attempts are counted in usage and may be billed; provider-hosted
tools can also have run on the discarded attempt. This prevents duplicate
**downstream** text/tool calls, not duplicate provider-side execution. No Source
order, credentials, or backend settings are changed by recovery. The initial
response buffer uses at most 256 KiB of RAM before spilling to temporary disk,
with a 128 MiB total cap. A disk error or an oversized response ends locally
without another replay.

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
| `~/.vibe_remote/state/backups/` | Automatic state backups taken before migrations |
| `~/.vibe_remote/state/agent_core/model-requests/` | Private copies of Vibey model requests that did not succeed (`vibe model-requests`); kept 7 days, 256 MiB at most |
| `~/.vibe_remote/runtime/remote-access-cloudflared.pid` | cloudflared tunnel PID for Avibe Cloud remote access |
| `~/.vibe_remote/screenshots/` | Default output directory for `vibe screenshot` |
| `~/.vibe_remote/logs/vibe_remote.log` | Application logs |
| `~/.vibe_remote/runtime/opencode/generations/` | One record per running OpenCode server generation |

### Migration Backup Lifetime

Automatic SQLite migration backups are temporary upgrade rollback points, not
long-term archives. A backup becomes eligible for deletion 72 hours after its
migration commits, database validation passes, and the core service reports
readiness. Restarting the service does not extend this deadline. The last
eligible copy can be deleted too.

The core service checks on startup and hourly, including when trace retention
is disabled. An offline installation catches up after startup. The existing
rollback-position count limit still applies independently, so older copies can
leave that window before 72 hours.

Failed, interrupted, restored, or unconfirmed migration attempts are not
age-expired. Legacy backups without recorded success evidence retain their
existing count policy; their creation dates are not used to invent an expiry
deadline. JSON import backups, manual archives, and unrecognized files are not
covered by this age policy. Restoring a SQLite backup revokes the old expiry
evidence before replacing the live database. Keep a separate manual archive
when recovery must remain possible beyond the automatic rollback window.

## See Also

- [Slack Setup Guide](SLACK_SETUP.md)
- [Telegram Setup Guide](TELEGRAM_SETUP.md)
- [Codex Setup Guide](CODEX_SETUP.md)
