# V2 Web UI Product Spec

Status: Final

This spec defines the local Web UI shipped in V2. It is launched by the `vibe` CLI, runs on `localhost`, and is the primary surface for:

- First-time setup (SaaS OAuth or self-host Socket Mode)
- Ongoing management (service controls + channel settings)
- Diagnostics (Doctor panel, aligned with `vibe doctor`)

It must align with:

- `docs/plans/v2/05_config_model.md`
- `docs/plans/v2/08_web_ui_flow.md`
- `docs/plans/v2/09_cli_and_install.md`
- `docs/plans/v2/10_slack_permissions.md`

---

## Goals

- Provide a guided, low-friction setup wizard that results in a valid local `~/.vibe_remote/` configuration.
- Support both deployment modes:
  - SaaS: official Slack app + OAuth + cloud relay pairing
  - Self-host: Slack Socket Mode + user-provided tokens
- Make service status obvious and controllable (start/stop/restart) from the dashboard.
- Make channel-level configuration intuitive: enable/disable per channel, set working directory, choose agent backend, and tune backend options.
- Provide a first-class Doctor panel that surfaces actionable diagnostics (the same checks as `vibe doctor`).
- Keep the UI safe-by-default:
  - Localhost-only
  - No message content persistence
  - Clear warnings before destructive actions (reset)

## Non-goals

- A hosted, multi-user, remotely accessible admin console.
- A full task/run history UI or “Vibe-native app” (future phases).
- Editing `sessions.json` from the UI.
- Multi-workspace support (V2 is single-workspace only).
- Supporting non-Slack IM platforms in V2.

## Users & key scenarios

- New user (SaaS): installs official Slack app via OAuth, pairs a local gateway, enables a few channels, starts service.
- New user (self-host): creates Slack app from manifest, pastes tokens, validates scopes/events, enables channels, starts service.
- Existing user: changes the default Agent or per-channel routing, updates `custom_cwd`, or changes which channels are enabled.
- Troubleshooting: bot not responding, gateway offline, missing Slack scopes, invalid tokens, executor CLI missing; user opens Doctor panel to diagnose and apply fixes.

## Permissions / roles

- Local operator (the person running `vibe`): full read/write access to local config and service controls.
- Slack workspace admin: required to install apps and create/configure Slack apps (self-host flow).
- Regular Slack member: can use the bot in channels after setup, but does not configure the gateway.

There is no in-product user account system in V2; access is implicitly the local machine user.

---

## Proposed solution

### Information architecture (routes)

The UI is a single-page app served by the local gateway.

- `/setup` (wizard; only shown when setup is incomplete)
- `/dashboard` (overview + service controls)
- `/channels` (channel list + quick toggles)
- `/channels/:channel_id` (channel detail/settings)
- `/doctor` (diagnostics)
- `/logs` (read-only log viewer + file path)
- `/advanced` (reset, file locations, ports)

Navigation rules:

- If config is missing or invalid: land on `/setup`.
- If config is valid: land on `/dashboard`.
- A persistent “Edit setup” action is available from Dashboard that re-enters the wizard with current values prefilled.

### Setup wizard (step-by-step)

Wizard is a linear stepper with back/next, autosave per step, and explicit validation before advancing.

Global wizard behavior:

- Autosave: when the user clicks “Continue”, the UI persists that step’s values via API; on refresh, the wizard resumes.
- Draft safety: values are stored in config files immediately, but service is not started until the final step.
- Inline errors: validation errors appear next to fields and in a summary banner at the top.
- Primary CTA is always right-aligned; secondary actions left-aligned.

#### Step 1: Welcome

Purpose: set expectations and confirm this is a local-only UI.

Content requirements:

- Headline: “Set up Vibe Remote on this machine”
- Bullets:
  - “Runs locally. Your code stays on your computer.”
  - “SaaS mode uses a cloud relay for Slack delivery, but does not store message content.”
  - “Self-host mode runs entirely locally via Slack Socket Mode.”
- CTA: “Get started”

#### Step 2: Choose mode

Collects: `config.mode`

UI:

- Two radio cards:
  - “SaaS (recommended)”
    - Subtext: “Fastest setup: OAuth install + cloud relay + local execution.”
  - “Self-host”
    - Subtext: “Use your own Slack app + Socket Mode tokens.”
- CTA: “Continue”

Rules:

- Switching modes resets Slack-specific fields for the other mode in-memory; on save it clears the unused fields from config (see Data & rules).

#### Step 3: Local executors

Collects: `config.agents.*` backend runtime settings. The legacy `config.agents.default_backend` field is deprecated.

UI requirements:

- Section: “Default Agent”
  - Dropdown: enabled Vibe Agents
  - Helper text: “Used when a channel does not override routing.”

- Cards for each backend:
  - Toggle: Enabled
  - Text input: CLI path
  - Button: “Detect” (runs local detection)
  - Inline status:
    - “Found: <version>”
    - or “Not found” with fix guidance

Backend-specific fields:

- OpenCode:
  - Optional text: Default agent (`config.agents.opencode.default_agent`)
  - Optional text: Default model (`config.agents.opencode.default_model`)
  - Dropdown: Default reasoning effort (`low|medium|high|xhigh`)
- ClaudeCode:
  - Optional text: Default model (`config.agents.claude.default_model`)
- Codex:
  - Optional text: Default model (`config.agents.codex.default_model`)

Defaults:

- OpenCode enabled by default.
- Default Agent comes from the Agent catalog.

Validation:

- At least one backend must be enabled.
- For each enabled backend, `cli_path` must be present and executable.

Microcopy:

- If the selected Agent's CLI is missing: show the missing backend CLI and ask the user to install it or switch Agents.

#### Step 4: Slack configuration

This step is mode-specific.

##### Step 4A: SaaS (OAuth + relay pairing)

Collects: `config.slack.team_id`, `config.slack.team_name` (read-only display), `config.gateway.relay_url`, `config.gateway.workspace_token`, `config.gateway.client_id`, `config.gateway.client_secret`.

UI:

- Card: “Connect your Slack workspace”
  - Button: “Connect Slack”
  - Opens system browser to cloud OAuth URL (provided by local API).

- After OAuth begins, show a progress state:
  - “Waiting for Slack authorization…”
  - Spinner + “Check status” is automatic polling.

- After OAuth completes:
  - Display workspace:
    - Team name and Team ID
    - Slack app ID (if provided)
  - Display relay pairing:
    - Relay URL
    - Workspace token (masked by default; reveal requires click)
    - Gateway client id

- CTA: “Continue”

Rules:

- Single workspace constraint: if `config.slack.team_id` is already set, this step shows “Connected to <team_name>” and disables “Connect Slack” unless the user resets workspace (Advanced).

Validation:

- OAuth completion must yield a workspace token and relay URL.
- Relay URL must be HTTPS.
- Workspace token must be present (non-empty) and stored locally.

##### Step 4B: Self-host (Socket Mode)

Collects: `config.slack.bot_token`, `config.slack.app_token`, optional `config.slack.signing_secret`, and optional `config.slack.app_id`.

UI sections:

1) “Create your Slack app”

- Show an App Manifest the user can paste into Slack (“From an app manifest”).
- Provide a copy-to-clipboard button.

Manifest (YAML):

```yaml
display_information:
  name: Vibe Remote (Self-host)
  description: Local-first agent runtime for Slack
  background_color: "#0B1B2B"
features:
  bot_user:
    display_name: Vibe Remote
    always_online: false
  slash_commands:
    - command: /start
      description: Open main menu
      should_escape: false
    - command: /stop
      description: Stop current session
      should_escape: false
oauth_config:
  scopes:
    bot:
      - channels:history
      - channels:read
      - chat:write
      - app_mentions:read
      - users:read
      - commands
      - groups:read
      - groups:history
      - groups:write
      - chat:write.public
      - files:read
      - files:write
      - reactions:read
      - reactions:write
      - users:read.email
      - team:read
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.groups
      - app_mention
      - member_joined_channel
      - member_left_channel
      - channel_created
      - channel_renamed
      - team_join
  socket_mode_enabled: true
  interactivity:
    is_enabled: true
``` 

2) “Add Socket Mode token”

- Text input: App token (`xapp-...`)
- Helper: “Create an app-level token with scope `connections:write`.”

3) “Add Bot token”

- Text input: Bot token (`xoxb-...`)

4) Optional: “Signing secret (optional)”

- Text input: signing secret
- Helper: “Socket Mode does not require a signing secret, but it can be used for additional verification.”

5) “Validate Slack connection”

- Button: “Run auth.test”
- Shows result:
  - team name/id
  - bot user id
  - missing scopes list (if any)

Validation:

- `bot_token` must start with `xoxb-`.
- `app_token` must start with `xapp-`.
- `auth.test` must succeed.

#### Step 5: Channel settings

Collects:

- `settings.channels[*].enabled` is the single source of truth for channel availability.
- Channel list loads and user can enable/disable channels; resulting `settings.json` is persisted.
- Confusing channel enable semantics are avoided by removing the global allow-list.
