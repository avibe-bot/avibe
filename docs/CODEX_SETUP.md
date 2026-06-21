# Codex Agent Setup

Avibe can route individual chat scopes to Codex instead of Claude Code. (OpenCode is also supported and recommended; see README for quick enablement.) This guide walks through enabling Codex end-to-end.

## 1. Install and authenticate Codex CLI

```bash
npm install -g @openai/codex
# or: brew install --cask codex
# or: download a release binary from https://github.com/openai/codex/releases
codex --help           # verify installation
codex                  # sign in when prompted
```

Codex CLI must be available on the PATH of the host running Avibe. The bot automatically runs Codex with `--json` and `--dangerously-bypass-approvals-and-sandbox`, so make sure you trust the workspace it operates in.

## 2. Configure environment variables

In `~/.vibe_remote/config/config.json`:

```json
{
  "agents": {
    "codex": {
      "enabled": true,
      "cli_path": "codex",
      "default_model": "gpt-5-codex"
    }
  }
}
```

No additional flag is required to bypass approvals—the bot always adds `--dangerously-bypass-approvals-and-sandbox`.

## 3. Route chats to Codex

Configure routing via the platform **Agent Settings** UI: pick Codex for the Slack channel, Discord channel, Telegram chat, or other scope you want.

Each routed scope stores a Vibe Agent name. Unrouted scopes fall back to the configured default Vibe Agent.

## 4. Restart the bot and test

```bash
vibe
```

In a routed chat, send a normal non-command message; you should see the bot acknowledge the request and then reply from Codex. For Telegram groups and forums, the default setup usually requires an explicit command, @mention, or reply-to-bot unless you already disabled `require_mention`. If the CLI is missing, the bot will reply with “Agent `codex` is not configured”.

## 5. Troubleshooting

- **“Agent `codex` is not configured”**: ensure `codex` CLI is installed and on PATH; check `CODEX_ENABLED`.
- **`codex exec` errors**: inspect the latest `~/.vibe_remote/logs/vibe_remote.log` and the in-chat error details.
- **Routing not applied**: confirm the target scope is the one you configured in **Agent Settings** for Slack, Discord, Telegram, or the relevant platform.
