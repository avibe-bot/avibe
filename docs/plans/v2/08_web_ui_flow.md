# Web UI Setup Flow (V2)

This document defines the local setup UI flow, covering SaaS and self-host modes.

## Entry

- CLI command: `vibe`
- Starts a local web server and opens the browser.

## Page Outline

1. Welcome
2. Mode selection (SaaS / Self-host)
3. Local agent executors (OpenCode / ClaudeCode / Codex)
4. Slack configuration (mode-specific)
5. Channel-level settings
6. Validation + finish
7. Start service

## SaaS Mode

### OAuth callback

- Slack OAuth requires HTTPS and a public domain.
- Default flow: OAuth redirects to cloud, then the local UI polls cloud for status.
- Optional: if Slack supports localhost redirect for the installed app, the UI can handle direct callback.

### Workspace binding

- Show team name/id, bot user id, and connection status.
- Display the relay binding token and gateway status.

### Channel list

- Fetch the list of channels where the bot is present.
- For each channel, allow per-channel configuration:
  - Enable/disable bot responses.
  - Assign local work path.
  - Select agent backend (OpenCode / ClaudeCode / Codex).
  - Configure backend-specific options (model, reasoning effort, agent).

## Self-host Mode (Socket Mode)

- Provide an auto-generated Slack App Manifest (copy button).
- Guide the user to create the app and install it.
- Ask for `xoxb-` and `xapp-` tokens.
- Signing secret is optional (Socket Mode does not require it).
- Validate with `auth.test` and a Socket Mode connection check.

## Local Executors

- Detect CLI paths automatically with a "Detect" button.
- Allow manual override.
- The default Agent is selected from the Agent catalog.

## Validation

- Slack token validation (`auth.test`).
- Optional test message to a selected channel.
- Report missing scopes or misconfigurations.

## Single Workspace Constraint

- V2 supports only one workspace binding.
- The UI should block additional workspace installs until reset.
