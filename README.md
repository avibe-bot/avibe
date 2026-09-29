<div align="center">

<img src="assets/logo.png" alt="Avibe" width="120"/>

# Avibe

### The local-first Agent OS — your AI partner lives on your own machine.

**Every agent. Every subscription. Any screen. Your machine.**

[![GitHub Stars](https://img.shields.io/github/stars/avibe-bot/avibe?color=ffcb47&labelColor=black&style=flat-square)](https://github.com/avibe-bot/avibe/stargazers)
[![Release](https://img.shields.io/github/v/release/avibe-bot/avibe?labelColor=black&style=flat-square)](https://github.com/avibe-bot/avibe/releases/latest)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?labelColor=black&style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green?labelColor=black&style=flat-square)](LICENSE)

<a href="https://www.producthunt.com/products/vibe-remote?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-vibe-remote" target="_blank" rel="noopener noreferrer"><img alt="Avibe — the local-first Agent OS | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1104967&theme=light&t=1774450119248"></a>

[Docs](https://docs.avibe.bot) · [English](README.md) · [中文](README_ZH.md)

**Drives** ![Claude Code](https://img.shields.io/badge/Claude%20Code-D4A27F?style=flat-square&logo=anthropic&logoColor=white) ![OpenCode](https://img.shields.io/badge/OpenCode-00B4D8?style=flat-square) ![Codex](https://img.shields.io/badge/Codex-412991?style=flat-square)

**Reach it from** ![Browser](https://img.shields.io/badge/Browser-111827?style=flat-square&logo=googlechrome&logoColor=white) ![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat-square&logo=slack&logoColor=white) ![Discord](https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white) ![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=flat-square&logo=telegram&logoColor=white) ![WeChat](https://img.shields.io/badge/WeChat-07C160?style=flat-square&logo=wechat&logoColor=white) ![Lark](https://img.shields.io/badge/Lark%20%2F%20Feishu-3370FF?style=flat-square&logo=bytedance&logoColor=white)

</div>

<br/>

<img src="assets/screenshots/v4/workbench-chat-en.png" alt="The Avibe Workbench — chat with a local agent, inspect its work, and respond without leaving the browser" />

---

## Your AI agent is brilliant — and stuck

Claude Code, Codex, and OpenCode are incredible. But:

- 🖥️ **Trapped on one machine.** Your agent lives in a terminal. Close the laptop and it stops.
- 📵 **Out of reach.** Away from your desk, you can't see what it's doing — let alone steer it.
- 💸 **Capped at the worst moment.** One plan hits its limit mid-task while another sits idle, and you can't tell whether either is paying for itself.
- 🔒 **Locked in.** Every tool wants to be the whole stack: its app, its cloud, its subscription, your code on someone else's box.

## Avibe flips that

**One command turns your own machine into the home your AI partner lives in.** Drive the *official* Claude Code, Codex, and OpenCode from a browser, your phone, or any chat app. Pool your subscriptions and API keys behind one local gateway. Your code, keys, and agent processes stay on your machine — `avibe.bot` handles sign-in and a secure tunnel, never your workspace.

```bash
bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --launch'
```

The browser opens and a short setup wizard finds your agents — installing any that are missing — connects your models, and drops you into the Workbench.

> Open source — read the [install script](https://github.com/avibe-bot/avibe/blob/master/install.sh) first if you like. The short URL is a 307 redirect to that file.

<details>
<summary><b>On Windows?</b></summary>

We recommend WSL on Windows for the best compatibility — see [Run Avibe with WSL from scratch](docs/WINDOWS_WSL.md). It covers where to install WSL, which terminal to use, where to run the install command, and how to open the Web UI.
</details>

> 💚 **Built with Avibe.** This project was developed end-to-end using Avibe itself — steering Claude Code, Codex, and OpenCode from the browser and from my phone, picking up seamlessly whether I was at my desk or not. The deeper in I got, the faster it went. — [@alex_metacraft](https://x.com/alex_metacraft)

---

## What you get

### 💬 A Workbench, not another chat box

Chat, files, editor, and terminal in one windowed workspace — with the apps and Show Pages your agent builds open right beside them. Your browser stops being a settings dashboard and starts feeling like an operating system.

<img src="assets/screenshots/v4/apps-library-en.png" alt="Avibe's multi-window Workbench with Files, Terminal, and Editor open together" />

### 🔀 Model Hub — every subscription, one gateway

Sign in your subscriptions and add your API keys once — official vendors, relays, aggregators, or self-hosted endpoints. Avibe's local gateway routes every Agent's models to them in the order you choose.

- **Keeps working when a plan runs dry.** If a source hits a quota, rate limit, or network failure, the next source in the route takes over. Once your first choice recovers, the next turn goes back to it.
- **One pool for every agent.** Claude Code, Codex, and OpenCode can share the same sources, each with its own route per model.
- **See what you're really getting.** Usage trends by model and source, live 5-hour and weekly quotas, and what your usage would cost at API prices — shown as how many times each plan has paid for itself.
- **Moves in with one click.** Already signed in to a supported CLI? Bring that login over with explicit consent. Credentials and routes stay on your machine.

### 🧠 Its own timeline — the Agent Harness

Most AI tools only move when you type. Avibe gives your agent four durable primitives — **run, schedule, watch, and inspect** — so it can start work, wait for the right moment, run in the background, and come back with results. Scheduled shell commands stay silent when they pass and can hand a failure to an Agent.

You don't need to learn the flags. Just ask:

- *"Watch this PR and come back when there's actionable review feedback."*
- *"Run the deployment check every weekday morning and post the summary here."*
- *"Start a separate investigation session for this incident, but report the conclusion to this channel."*
- *"If CI fails, summarize the logs; if it passes, tell me whether the PR is mergeable."*

<img src="assets/screenshots/v4/harness-tasks-en.png" alt="Agent Harness task list with schedule, Agent ownership, session, and delivery details" />

### 🤖 Bring the agents you already trust

Run the official Claude Code, Codex, and OpenCode CLIs — not re-implementations — behind one Agent registry. Pick the model and reasoning effort per Agent, and route each project or channel to the right specialist. Write a Skill once and Avibe loads it the same way for all three backends.

When one agent hands work to another, **Runs** draws the collaboration as a graph: who started each background Session, where it reports back, and the history behind every node.

<img src="assets/screenshots/v4/agents-graph-en.png" alt="Avibe Agent run graph showing a parent Session delegating work to Claude Code, Codex, and OpenCode background Sessions" />

### 🎨 Show Pages — when a picture beats a paragraph

Your agent hands you a live web page — a dashboard, flowchart, diff, report, or small app. Click an element, box a region, or mark up a screenshot, say what you want, and the agent reworks the page or answers right where you pointed.

Keep a page private, share it with specific people, or publish it at a memorable link. Pin the ones you use to the Dock and they open as apps.

<img src="assets/screenshots/v4/show-page-en.png" alt="Show Page review with anchored comments and Agent replies on the page" />

### 📱 Your phone, your chat apps, any browser

<img src="assets/screenshots/v4/workbench-mobile-en.png" alt="Avibe Workbench on mobile" width="270" align="right" />

Your machine does the work; you don't have to sit in front of it. Use the Workbench when you want windows, or Slack, Discord, Telegram, WeChat, and Lark / Feishu when chat is faster — they reach the same Agents and the same sessions.

- 🔔 **It taps you on the shoulder.** Install the Workbench as an app on your phone or desktop and get a push notification the moment a job needs you.
- 🎙️ **Talk instead of type.** Realtime voice input writes as you speak, then tidies the transcript in place. On desktop, press ⌥Z (Alt+Z on Windows and Linux).
- 🌍 **Your own `you-app.avibe.bot`.** Run `vibe remote` and your local Workbench is reachable from any browser — no VPN, no port forwarding.
- 🔒 **Only the people you invite.** Remote sign-in is limited to the emails you authorize, and auth, routing, and host checks all fail closed.

You're on a plane, at a café, on a borrowed laptop. The agent pings you. Open the link, steer it, walk away again.

<br clear="all"/>

### 🔐 Vaults — secrets by name, never by value

Add an API key or token once. Agents request it by name, you approve from the browser, and Avibe delivers it to the command, authenticated request, or signing operation that needs it. Vault responses never include the secret value; commands receiving it must still avoid printing it. Passkey-protected custody is in preview.

**Plus** — thread = session, resumable anywhere · interactive buttons and forms when the agent needs a decision · rich attachments, including inline audio and video · keyboard shortcuts · one-click backend updates.

---

## Why Avibe is different

| | |
|---|---|
| **Local-first, and yours** | Your AI partner, its execution, your keys, and your code stay on your machine. `avibe.bot` issues identity and a secure tunnel; it never proxies your workspace. |
| **Every first-party agent, one home** | Drive the *official* Claude Code, Codex, and OpenCode. Switch per task, per project, or per channel — no vendor silo. |
| **Every subscription, working together** | Model Hub pools your plans and keys, fails over automatically, and shows what each one is worth. |
| **Browser and chat, both first-class** | Workbench, phone, Slack, Discord, Telegram, WeChat, and Lark / Feishu — same agent, same sessions. |
| **No middleman** | No extra reasoning loop sits between you and your agent. Tokens go straight to the agent you chose. |

---

## How it works

```
  You, anywhere              Your machine                            Your models
┌──────────────┐      ┌──────────────────────────────────────┐      ┌──────────────┐
│ Browser      │      │ Avibe                                │      │ Subscriptions│
│ Phone app    │ ───▶ │   Claude Code ─┐                     │      │ API keys     │
│ Slack        │      │   Codex       ─┼─▶ Model Hub ────────┼────▶ │ Relays       │
│ Discord …    │ ◀─── │   OpenCode    ─┘                     │      │ Self-hosted  │
└──────────────┘      └──────────────────────────────────────┘      └──────────────┘
```

1. **You ask** — in the browser or a chat app: *"Add dark mode to the settings page."*
2. **Avibe routes** the message to the right Agent, in the right project.
3. **The agent** reads your local codebase, writes code, and streams its work back. Model Hub picks the source for each call.
4. **You review** in the same surface, iterate in the thread, and pick it up later from anywhere.

Avibe connects out via Slack Socket Mode, Discord Gateway, Telegram long-polling, WeChat polling, or Lark WebSocket — no public inbound ports for chat control. Agent prompts go only to the model sources you configure.

---

## Avibe vs OpenClaw

| | Avibe | OpenClaw |
|---|---|---|
| **Setup** | One command + web wizard. Done in minutes. | Gateway + channels + JSON config. Expect an afternoon. |
| **Security** | Local-first. Socket Mode / WebSocket only. No public inbound ports, minimal attack surface. | Gateway exposes ports. More moving parts, more surface. |
| **Token cost** | No extra reasoning loop in between. Tokens go straight to your chosen agent. | Every message carries a long persona/orchestration context. Tokens burn on overhead before your task starts. |
| **Lock-in** | Drives the official agent CLIs; bring your own subscriptions and keys; switch per task. | Tied to its own assistant loop. |

OpenClaw is an always-on personal assistant. Avibe is the **Agent OS** for the agents you already trust: the agent stays itself, your data stays local, and the colleague experience comes from putting the agent where your work already happens.

---

## FAQ

<details>
<summary><b>Can I use my existing Claude or ChatGPT subscription?</b></summary>

Yes. Claude Code and Codex can keep their own logins, or you can add supported subscriptions to Model Hub as gateway sources to get quota tracking, usage analytics, and automatic takeover. Each vendor's terms still apply; Avibe warns you before a sign-in that may carry account restrictions. See [Model Hub](https://docs.avibe.bot/concepts/model-hub).
</details>

<details>
<summary><b>Does it run local models?</b></summary>

Local-first here means your **code, data, and execution** stay on your machine — not necessarily the model weights. Model Hub can point at any compatible self-hosted endpoint if you want inference local too.
</details>

<details>
<summary><b>Where do my code and data go?</b></summary>

Your code, keys, and agent processes stay on your machine. Agent prompts go to the model sources you configure. `avibe.bot` handles sign-in and the Remote Web UI tunnel without proxying your workspace; Voice input is the exception that goes through `avibe.bot` for transcription. The [boundary inventory](https://docs.avibe.bot/concepts/local-first#what-leaves-your-machine) lists every network path.
</details>

<details>
<summary><b>Do I have to pay for Avibe?</b></summary>

Avibe is open source (MIT) and free to run. You bring your own agent subscriptions or API keys and pay your provider directly — no markup, no second subscription.
</details>

<details>
<summary><b>Which agents and platforms are supported?</b></summary>

**Agents:** the official Claude Code, Codex, and OpenCode CLIs. **Surfaces:** a built-in browser Workbench (installable on desktop and mobile) plus Slack, Discord, Telegram, WeChat, and Lark / Feishu.
</details>

<details>
<summary><b>Is remote access secure?</b></summary>

`vibe remote` runs a Cloudflare tunnel from your machine; browser traffic reaches it only after sign-in, and only for the emails you authorize. Auth, routing, and host checks are **fail-closed**, and there are no public inbound ports for chat control.
</details>

<details>
<summary><b>How is Avibe different from OpenClaw or Hermes?</b></summary>

OpenClaw and Hermes are *agents* — a gateway-style assistant and a self-improving agent. Avibe is a different layer: the **Agent OS**. It gives any agent a unified world model — agents, sessions, Show Pages, the Harness, Model Hub — so it can schedule itself, build its own loops, and reach you through a real interaction layer, then runs the official Claude Code, Codex, and OpenCode you bring (an Avibe-native agent is [on the roadmap](#roadmap)). See the [comparison table](#avibe-vs-openclaw) above for OpenClaw specifics.
</details>

---

## Meet Vibey

<div align="center">
<img src="assets/mascot/cloud-tuanzi.png" alt="Vibey — the gaseous consciousness inside Avibe" width="200"/>
</div>

Lives in your Workbench and your chat apps. Reads the room. Picks up where you left off. Asks the right question when it's unsure. Goes quiet when you're heads-down. Ships at 2am because that's when the vibe hits — then leaves a note about what it touched.

> Avibe is the home your agent lives in. Vibey is the colleague who lives there.

Keeps receipts. Holds opinions. Says thanks when you fix its bugs.

---

## Commands

```bash
vibe            # Start Avibe and open the Workbench
vibe status     # Check service and configuration status
vibe stop       # Stop the local service
vibe upgrade    # Upgrade to the latest release
vibe doctor     # Diagnose common setup issues; "vibe doctor repair" applies safe fixes
vibe remote     # Reach your Workbench from any device via avibe.bot
vibe agent      # Run and manage Avibe Agents
vibe task       # Schedule time-based work (cron / one-off)
vibe watch      # Wait on a condition, then act
vibe runs       # Inspect agent run history
vibe show       # Create, inspect, and publish Show Pages
vibe vault      # Manage secrets, requests, authenticated fetches, and signing
```

| In chat | What it does |
|---|---|
| Mention the bot | Start a task or ask a question |
| Reply in thread | Continue the same agent session |
| `/stop` | Stop the current session |

Full references: [Commands](docs/COMMANDS.md) · [CLI](docs/CLI.md)

---

## Agents

The setup wizard detects Claude Code, Codex, and OpenCode on your machine and installs any you choose that are missing. To install one yourself:

<details>
<summary><b>Claude Code</b></summary>

```bash
npm install -g @anthropic-ai/claude-code
```
</details>

<details>
<summary><b>Codex</b></summary>

```bash
npm install -g @openai/codex
```
</details>

<details>
<summary><b>OpenCode</b></summary>

```bash
curl -fsSL https://opencode.ai/install | bash
```

OpenCode asks for approval on every tool call unless `~/.config/opencode/opencode.json` allows them; the setup wizard can set this for you:

```json
{ "permission": "allow" }
```
</details>

---

## Security

- **Local-first** — Avibe runs on your machine; your code and agent processes stay there.
- **No public inbound ports** — Socket Mode / WebSocket / long-polling only for chat control.
- **Your keys, your data** — stored under `~/.avibe/`, sent only to the model sources you configure. Existing installs keep `~/.vibe_remote/` as a compatibility path.
- **Fail-closed remote access** — `avibe.bot` brokers identity and the tunnel, never your workspace.

---

## Uninstall

```bash
vibe_bin="$(command -v vibe)"   # capture Avibe's launcher before uninstalling
"$vibe_bin" stop
avibe_home="${AVIBE_HOME:-$HOME/.avibe}"
avibe_home="${avibe_home/#\~/$HOME}"
uv tool uninstall avibe-os
uv tool uninstall vibe-remote   # legacy install
rm -f "$vibe_bin" "$(dirname "$vibe_bin")/.vibe.avibe-generation"
rm -rf "$avibe_home/runtime/install-generations"
rm -rf "$avibe_home" ~/.vibe_remote
```

---

## Roadmap

What's coming next:

- **A native desktop app** — macOS and Windows builds with signed self-updates are in testing now.
- **Deeper interaction-first workflows** — more direct manipulation, structured decisions, and in-place collaboration between chat, apps, and Show Pages.
- **Protected Vault custody** — finish the immutable sandbox integrity gate before enabling passkey-protected secrets for real users.
- **SaaS mode** — one-click hosted onboarding with a cloud relay, while execution still stays on your own machine.
- **An Avibe-native agent** — a first-party agent tuned for this runtime, alongside the official CLIs you bring.

Shipped recently: Model Hub with automatic takeover, quota tracking, and usage analytics · realtime voice input · Show Page sharing · Skills unified across all three backends · the three-step setup wizard · the Agent Harness and run graph · multi-window Apps · Standard Vaults.

---

## Docs

- **[Official Docs](https://docs.avibe.bot)** — quickstart, concepts, platform & agent guides, troubleshooting
- **[What is Avibe](https://docs.avibe.bot/concepts/agent-os)** — the Agent OS model
- **[Model Hub](https://docs.avibe.bot/concepts/model-hub)** — sources, routes, takeover, usage, and quota
- **[CLI Reference](docs/CLI.md)** · **[Commands](docs/COMMANDS.md)** · **[Show Pages](docs/SHOW_PAGES.md)**
- **[Install via AI agent](docs/INSTALL_FOR_AI.md)** — hand this to Claude Code, Codex, or OpenCode for guided setup
- **[Slack](docs/SLACK_SETUP.md)** · **[Discord](docs/DISCORD_SETUP.md)** · **[Telegram](docs/TELEGRAM_SETUP.md)** setup guides

---

<div align="center">

**Own the agent. Reach it from anywhere.**

[Install Now](#avibe-flips-that) · [Docs](https://docs.avibe.bot) · [Report a bug](https://github.com/avibe-bot/avibe/issues) · [Follow @alex_metacraft](https://x.com/alex_metacraft)

</div>
