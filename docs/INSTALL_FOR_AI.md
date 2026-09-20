# Avibe Installation Guide for AI Agents

This file is designed to be handed directly to Claude Code, Codex, OpenCode, or another local coding agent. Use it to install and configure Avibe with the user.

Avibe connects local AI coding agents to chat platforms such as Slack, Discord, Telegram, WeChat, and Lark / Feishu. The user's data and agent processes stay on the user's machine.

## Rules for the Assisting Agent

- Ask the user before choosing a chat platform, agent backend, workspace, or credential value.
- Do not guess secrets, bot tokens, app tokens, API keys, workspace IDs, or chat IDs.
- Prefer the browser setup wizard launched by `vibe` over hand-editing config files.
- Do not restart an already-running local `vibe` service unless the user confirms it is safe.
- Keep setup local-first. Do not expose public webhooks unless the selected platform requires it.

## Step 1: Check the Machine

Run:

```bash
uname -a
command -v vibe || true
command -v uv || true
command -v claude || true
command -v opencode || true
command -v codex || true
```

On Windows, prefer the WSL guide unless the user explicitly wants native PowerShell:

- https://github.com/avibe-bot/avibe/blob/master/docs/WINDOWS_WSL.md

## Step 2: Install Avibe

macOS / Linux:

```bash
curl -fsSL https://avibe.bot/install.sh | bash
```

Open source — view the [script on GitHub](https://github.com/avibe-bot/avibe/blob/master/install.sh). The short URL is a 307 redirect to that file.

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1 | iex
```

Verify:

```bash
vibe version
vibe doctor
```

If the installer cannot use PyPI, it falls back to GitHub. If `vibe` is installed but not on `PATH`, inspect the installer's final output and help the user add the reported bin directory to their shell profile.

## Step 3: Install at Least One Coding Agent

Ask the user which backend they want first. Recommended default: OpenCode for low-friction setup, Claude Code for deeper coding work, Codex for OpenAI workflows.

OpenCode:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Then configure permission behavior if the user accepts the tradeoff:

```json
{
  "permission": "allow"
}
```

Claude Code:

```bash
npm install -g @anthropic-ai/claude-code
```

Codex:

```bash
npm install -g @openai/codex
```

Verify the selected agent:

```bash
opencode --version || true
claude --version || true
codex --version || true
```

## Step 4: Start the Setup Wizard

Run:

```bash
vibe
```

This starts the local service and opens the Web UI setup wizard. If a browser does not open automatically, check the terminal output for the local URL.

In the wizard, help the user install or detect Claude Code, Codex, or OpenCode,
then connect one assistant with a subscription or API key. Existing valid
credentials count; starting another login preserves them until the new login
commits. Settings → Backends uses the same connection and recovery controls.
OpenCode retains its explicit **Allow tool calls** action.

Select **Enter workspace** explicitly. A stopped service starts at this point;
the wizard confirms application and an available Agent before completing setup.
Pending application, uncertain keychain auth, or a failed start stays recoverable
on this screen. For OpenCode, if the Agent's provider is unconnected or its exact
model is absent from that provider's merged native/custom catalog, choose a
compatible model in the inline recovery and apply it explicitly. The wizard never chooses a model or changes an existing usable default silently.

IM is optional for a fresh installation. Configure Slack, Discord, Telegram,
WeChat, or Lark / Feishu later in Settings. Existing enabled but incomplete IM
configuration opens an explicit **Repair saved messaging configuration** action
using the existing platform form. Repair only its reported missing credentials;
setup does not disable adapters or erase their configuration to bypass validation.

Platform docs:

- Slack: https://github.com/avibe-bot/avibe/blob/master/docs/SLACK_SETUP.md
- Discord: https://github.com/avibe-bot/avibe/blob/master/docs/DISCORD_SETUP.md
- Telegram: https://github.com/avibe-bot/avibe/blob/master/docs/TELEGRAM_SETUP.md
- WeChat: use the in-app wizard.
- Lark / Feishu: use the in-app wizard.

## Step 5: Optional Remote Web UI

If the user wants to open the local Web UI from a phone, tablet, or remote machine, run:

```bash
vibe remote
```

This guides the user through avibe.bot sign-in, pairing, and a secure tunnel. Use this for Web UI access, not for exposing the agent runtime directly.

## Step 6: Smoke Test

After setup, open the Web workspace, select an available Agent, and ask the user
to send a short message in a new conversation:

```text
Say hello and tell me which project directory you are running in.
```

Then verify:

```bash
vibe status
```

If the Web response does not arrive, inspect the selected Agent, backend
connection and application status in Settings, then run:

```bash
vibe doctor
```

Only if an IM platform was explicitly configured, repeat the message in an enabled
IM chat. For IM delivery failures, check the platform-specific docs for missing
permissions, bot privacy settings, or unselected channels. A Web-only installation
does not need an IM chat to complete this smoke test.

## Common Fixes

| Symptom | What to check |
| --- | --- |
| `vibe` command missing | Shell `PATH`, uv tool bin directory, installer output |
| Agent not found | Install the selected agent CLI and verify it is on `PATH` |
| Slack bot silent | App installed, Socket Mode token, bot token scopes, bot invited to channel |
| Telegram group silent | Bot privacy mode, mention requirements, discovered chat list |
| Discord silent | Bot token, guild/channel selection, gateway intents |
| Web UI unreachable from phone | Use `vibe remote`; localhost only works on the same machine |

## Uninstall

Only run this if the user asks to remove Avibe:

```bash
vibe stop
avibe_home="${AVIBE_HOME:-$HOME/.avibe}"
avibe_home="${avibe_home/#\~/$HOME}"
uv tool uninstall vibe-remote
vibe_bin="$(command -v vibe)" && rm -f "$vibe_bin" "$(dirname "$vibe_bin")/.vibe.avibe-generation"
rm -rf "$avibe_home/runtime/install-generations"
rm -rf "$avibe_home" ~/.vibe_remote
```
