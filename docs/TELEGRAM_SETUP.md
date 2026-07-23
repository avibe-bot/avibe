# Telegram Setup Guide

## TL;DR

```bash
vibe
```

Choose **Telegram** in the wizard, create a bot in **@BotFather**, paste the token, validate it, then finish the Telegram-side setup.

---

## Step 1: Create a Bot with BotFather

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot`
3. Choose a display name
4. Choose a username ending in `bot`
5. Keep the generated token page open

If you already created the bot, you can skip straight to copying the token.

---

## Step 2: Paste the Token in Avibe

1. Run `vibe`
2. In the setup wizard, choose **Telegram**
3. Paste the bot token from BotFather
4. Click **Validate Token**

If validation fails, make sure you copied the full token and did not include extra spaces.

---

## Step 3: Finish the BotFather Switches

Run these commands in **@BotFather** and select your bot each time:

- `/setprivacy`
- `/setjoingroups`
- `/setcommands`

Recommended settings:

- **`/setprivacy` -> `Disable`** if you want the bot to respond to normal group messages without explicit `@mentions`
- **`/setjoingroups` -> `Enable`** if the bot should be used in groups or forum-enabled supergroups
- **`/setcommands`** -> publish common commands such as `/start`, `/settings`, `/new`, `/resume`

The most common Telegram setup issue is leaving privacy mode enabled. In that state, the bot only receives commands, mentions, and replies.

---

## Step 4: Finish Setup, Bind, Then Discover Chats

Avibe discovers Telegram chats from inbound messages. Telegram does not provide a generic "list every chat the bot is in" API.

Important: Telegram DMs are usable, but they stay hidden in the wizard chat-selection UI. The wizard selects discovered groups and forum chats. After setup, the dashboard's **Group Settings** page also lists forum topics that the bot has seen.

### Configure individual forum topics

Open **Dashboard → Group Settings → Telegram**, then expand a forum group. Every discovered topic initially inherits the group settings. Choose **Customize** to give that topic its own:

- enabled state and `@mention` requirement
- bound-user-only access policy
- Vibe Agent, model, reasoning, and working directory
- visible message types

Choose **Use group settings** to delete the override and resume inheritance. Telegram does not provide a complete topic-list API, so a topic appears after the bot receives a message there. The General topic is treated as topic ID `1`.

On first setup, do this in order:

1. Finish the Avibe setup flow and start the service
2. On the final summary screen, copy the first bind command shown there
3. Open a DM with the bot and send `bind <code>` (or `/bind <code>`) to become the first admin
4. After binding, send `/start` in the DM to verify direct-message connectivity
5. If you want to use a group or forum, add the bot there and grant permission to send messages
6. For auto-created forum topics, also grant admin or topic-management rights
7. Send one message in each target group or forum chat; for forums, sending a message inside the forum helps Avibe discover that chat and later use topic-related behavior there
8. In the dashboard group settings page, refresh the Telegram chat list and enable the discovered group or forum chat

If the bot only reacts to commands in groups, go back and verify `/setprivacy` is really set to `Disable`.

---

## Step 5: Choose Telegram Defaults

The wizard exposes two important Telegram defaults:

- **Require explicit bot targeting in groups**
  - When enabled, the bot responds only to commands, mentions, or replies
  - Good for busy groups
- **Forum auto-topic mode**
  - In forum-enabled supergroups, a new top-level message can create a new topic automatically
  - Requires admin or topic-management rights for the bot

---

## Using in Telegram

### Direct Messages

1. Open your bot in Telegram
2. First send `bind <code>` (or `/bind <code>`) using the bind code shown by Avibe setup
3. Then send `/start`
4. Continue chatting normally

### Groups

1. Add the bot to the group
2. If `require_mention` is enabled, use `/start`, `@botname`, or reply-to-bot style messages
3. If `require_mention` is disabled, the bot can respond to normal group messages too

### Forum Topics

1. Add the bot to a forum-enabled supergroup
2. Ensure it has enough permissions
3. Send a message in the target topic so Avibe discovers it

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Token validation fails | Re-copy the token from BotFather and validate again |
| Bot works in DM but not in groups | Run `/setjoingroups` and confirm it is `Enable` |
| Bot only reacts to commands or `@mentions` | Run `/setprivacy` and set it to `Disable` |
| Group or forum does not show up in the wizard | Send one message there first, then refresh the Telegram chat list |
| Forum auto-topic does not work | Grant admin or topic-management rights to the bot |

**Logs:** `~/.vibe_remote/logs/vibe_remote.log`

**Diagnostics:** `vibe doctor`

## Using a Proxy

If Telegram API is blocked in your region, you can configure a proxy server. The easiest way is to set it up in the setup wizard: enter the proxy URL in the "Proxy URL" field before validating your bot token.

### Configuration via Setup Wizard

In the Telegram setup wizard (Step 2: Paste the Token), you'll see a "Proxy URL" field below the bot token input. Enter your proxy URL there and click "Validate Token" — the validation will use the proxy automatically.

### Configuration via Config File

Alternatively, add `proxy_url` to your config in `~/.vibe_remote/config/config.json`:

```json
{
  "mode": "self_host",
  "platforms": {
    "telegram": {
      "bot_token": "YOUR_BOT_TOKEN",
      "proxy_url": "socks5://user:password@proxy.example.com:1080"
    }
  }
}
```

### Supported Proxy Types

| Scheme | Type |
|--------|------|
| `socks5://` | SOCKS5 proxy |
| `socks4://` | SOCKS4 proxy |
| `http://` | HTTP proxy |
| `https://` | HTTPS proxy |

### If the Proxy Fails

Avibe does **not** automatically fall back to a direct connection when
the proxy is unreachable. Telegram requests will fail and the error will be
logged. To recover, fix the proxy, switch `proxy_url` to a working endpoint,
or remove the `proxy_url` field if Telegram is reachable directly.

### Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot can't connect | Check proxy URL format and credentials |
| Works then stops | Proxy is down — restart it or remove `proxy_url` to use a direct connection |
| Token validation fails | Verify proxy allows connections to api.telegram.org |
