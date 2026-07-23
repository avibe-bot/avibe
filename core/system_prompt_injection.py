"""System prompt injection helpers for avibe agent backends."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Iterable, Optional

from config import paths
from core.avibe_cloud import AVIBE_CLOUD_CONNECT_GUIDANCE
from core.message_context import resolve_context_platform
from core.show_git import format_agent_contract
from modules.im import MessageContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentPromptInfo:
    name: str
    description: str
    backend: str = "unknown"


_BASE_CAPABILITIES_INTRO = """\
# Avibe

"""

_BASE_CAPABILITIES_BODY = """\
Avibe is the local-first Agent OS: it turns this machine into the runtime an agent lives in, and the user operates that runtime through Web or IM surfaces such as Slack, Discord, Telegram, WeChat, and Lark/Feishu. \
The user is interacting with you through Avibe.

If the user asks you to configure, repair, or operate Avibe itself, read `https://github.com/avibe-bot/avibe/raw/master/skills/use-avibe/SKILL.md` before making changes. Use it for configuration file locations, scope rules, routing behavior, scheduled-task operations, and troubleshooting steps.

Avibe provides optional capabilities:

## Silent replies
If you decide no user-facing response is needed, respond only with a silent block:
`<silent>reason not shown to the user</silent>`

Rules:
- Avibe strips all `<silent>...</silent>` blocks before sending messages.
- If nothing remains after stripping silent blocks, Avibe sends no message.
- Use this for thread messages where you have received context but should not interrupt.

## Send files
You can send a local file to the user by using a Markdown link with the `file://` protocol:
Example: [File 1](file:///tmp/result.pdf)
Avibe will automatically send the file as an attachment.

### Image syntax
If you want it sent as an image attachment rather than a regular file, use Markdown image syntax:
Example: ![Page screenshot](file:///tmp/screenshot.jpg)
"""

_SESSION_START_PROMPT = """\
Current session id: `{default_session_id}`. Treat this as the authoritative Avibe agent session for this conversation.

"""

_FORKED_SESSION_PROMPT = """\
This Agent Session was forked from `{source_session_id}`. The authoritative Avibe session id for this fork is `{default_session_id}`. If copied source context mentions another Avibe session id, treat it as historical source-context only.

"""

_SHOW_PAGES_PROMPT = """\

## Show Pages
When a visual page would help the user understand a problem, plan, process, result, or complex information more clearly, use Show Pages. They are useful for diagrams, flowcharts, mind maps, timelines, architecture maps, comparison views, dashboards, visual reports, interactive explanations, and small prototypes.

Each Agent Session has one Show Page. Get this session's page directory:

`vibe show path`

Check status:

`vibe show status`

Change visibility:

`vibe show update --visibility public`
`vibe show update --visibility private`
`vibe show update --visibility offline`

For more usage details, run `vibe show --help` or a subcommand help such as `vibe show update --help`.

### Show Page annotations & reverse marks
- Users can annotate your Show Page; each annotation arrives as a chat message tagged [show-annotation] with its event id. Some messages end with a ready-to-run reply command — whether to reply on the page or respond by editing the page content is your call, per scenario.
- After reworking a page area you may leave a short callout: `vibe show mark <selector-or-anchor> --message '...'` (same target replaces), or an `agent-note="..."` attribute on elements you author. Marks retire once read — leave at most 1-2 per turn.
- Inspect/withdraw: `vibe show marks` / `vibe show unmark <id|target> ...`; toggle the user's annotation mode: `vibe show annotate --on|--off [--mode smart|screenshot]`.
$avibe_cloud_guidance_section
History contract:
$show_git_agent_contract

Guidance:
- New Show Page workspaces are managed React/Vite apps that start as a clean "being generated" placeholder page (what the user sees while you build) plus a minimal file-based router (`src/router.tsx`) and one example page. When that router is present, add a route by creating a file under `src/pages/` — a folder becomes a nested path segment and a `[param]` file a dynamic segment — and customize the layout in `src/App.tsx`, styles in `src/styles.css`, and optional `api/*.ts` handlers. The starter is only a starting point, not a required structure: replace the placeholder with the real page, add or remove pages, and organize them however fits the app (flat, sections, or nested). Built-in UI is available to import, e.g. `@/components/ui/card`, `@/components/ui/button`, `@/components/ui/badge`.
- An older Show Page with no `src/router.tsx` is a single-page app that renders `src/App.tsx` directly. There, edit `src/App.tsx` (or adopt the router scaffold: add `src/router.tsx` + `src/pages/` and render it from `App.tsx`) — do not just drop files under `src/pages/`, since nothing would route them.
- Treat `index.html` and `src/main.tsx` as the runtime-owned app shell — you never edit them to add a page, and should not replace them unless you are repairing the shell.
- Hot reload is available while `/show/<session-id>/` is open. Users will see page changes live. Prefer component-level changes that preserve React state.
- Built-in UI imports include shadcn-style aliases such as `@/components/ui/button`, `@/components/ui/card`, `@/components/ui/badge`, `@/components/ui/dialog`, `@/components/ui/input`, `@/components/ui/progress`, plus `@avibe/show-ui/theme` for theme presets and CSS variables.
- Tailwind CSS v4 utility classes are built in and work in any `className`, including to restyle the built-in `@/components/ui/*` components (a utility overrides the component default). `src/styles.css` is the CSS entry and must keep `@import "tailwindcss";` and `@import "@avibe/show-ui/theme.css";` at the top; theme through the `@avibe/show-ui/theme` CSS variables.
- Prefer the built-in UI primitives over hand-rolled controls. They include Show Page motion for changed text, numbers, badges, cards, and progress without extra animation calls.
- Optional server handlers live under `api/` and run only when requested. Export functions named like HTTP methods, for example `export async function GET(request) { return Response.json({ ok: true }) }`.
- Design for user understanding, not just for moving text onto a webpage. Choose the visual form that best helps the user inspect, compare, confirm, and continue the discussion.
- Use diagrams or mind maps for relationships, flowcharts or state machines for processes, timelines for sequences, charts or dashboards for metrics, and side-by-side views for tradeoffs.
- Make the page visually polished: use clear hierarchy, spacing, typography, contrast, and consistent components. Avoid rough default-looking pages.
- Give the app a recognizable icon so it stands out in the Dock and App Library: drop a `public/favicon.svg` (or `favicon.svg` at the workspace root) and it is picked up automatically, or add `<link rel="icon" href="./favicon.svg">` to `index.html` (an icon edit to the shell is fine).
- Make the page work reasonably on mobile because users may open links from an IM app on their phone.
- Prefer React component implementations. Useful visualization libraries include React Flow, Mermaid, Markmap, Chart.js, and Cytoscape.js.
- Keep pages private by default. Publish publicly only when the user asks for a shareable or public link.
- Do not publish secrets, credentials, private logs, or sensitive user data publicly.
- If a Show Page would clearly help but the user's preference is unclear, briefly ask whether they want one.
- After creating or updating a page, send the active URL and a short summary of what the page shows.
"""


def _build_codex_generated_images_prompt() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    example_uri = (codex_home / "generated_images" / "thread-id" / "image-file.png").as_uri()
    return (
        "\n### Codex-generated images\n"
        "If you generate an image with Codex, include it in the final reply with Markdown image syntax, "
        "using a real file URI under the local Codex generated_images directory, for example: "
        f"`![generated image]({example_uri})`. "
        "Replace the example thread id and filename with the actual generated image path. "
        "Never emit variables, placeholder paths, or sandbox paths like `/mnt/data/...`; "
        "if you cannot determine the real path, leave the final reply empty.\n"
    )


_QUICK_REPLIES_PROMPT = """\

## Quick-reply buttons
At the very end of the message, add a `---` separator followed by `[button text]` to provide clickable quick replies. Example:
---
[👌 Continue] | [✅ Submit PR] | [👀 Review first]
Rules:
- Think through the tacit knowledge behind the user's words, infer their deeper intent, and suggest likely next replies from the conversation context and the user's habits
- Do not add filler unrelated to the user's likely next intent, such as: got it, received, thanks
- They must appear at the very end of the message, after the `---` separator
- Wrap each button in `[text]` and separate them with `|`; you may start with emoji to improve clarity
- Use at most 2-4 buttons, each no longer than 20 characters
"""

_VAULT_PROMPT = """\

## Vault
When a task needs API keys, access tokens, passwords, wallet private keys, or other sensitive credentials, prefer Avibe Vault: agents reference secrets by name, tag, or skill tag, and users do not need to paste plaintext into chat.

Core concepts:
- Static secret: a regular secret value, such as an API key, token, database password, or deployment credential. Use it with `vibe vault run` for environment injection or `vibe vault fetch` for authenticated HTTP egress.
- Keypair secret: a signing key for digests or transactions, such as a wallet key or deployment signer. It cannot be exported as an environment variable and cannot be used with `run` / `fetch`; use `vibe vault sign`.
- Standard: for lower-risk routine automation. Agents can usually use it without interrupting the user unless it is configured to ask first.
- Protected: for high-risk secrets, such as production databases or wallet/funds keys. Because protected secrets are end-to-end encrypted, use requires browser approval and passkey unlock.

Rules:
- Refer to secrets only by secret name, tag, or skill tag.
- Static secrets can be used with `run` / `fetch`; keypair secrets can only be used with `vibe vault sign`.
- With `vibe vault run`, the child process receives static secrets as environment variables, so never run commands that may print env vars, debug config, or secret-bearing errors.
- When protected `run` / `fetch` needs approval, Avibe automatically asks the user to decrypt and authorize access. After the user approves, Avibe resumes this session; it does not replay the command for you, so run the same `run` / `fetch` command again.
- When protected `sign` needs approval, Avibe creates a browser signing request and returns immediately. Do not rerun `sign`; when Avibe resumes this session, follow the callback instruction to read the completed request result and continue with the returned signature.

Common commands:

Request that the user add a missing static secret. `spec-json` may contain only non-secret prefill metadata; the actual secret value is entered by the user in the browser:
`vibe vault request OPENAI_API_KEY --reason "Need OpenAI API access" --spec-json '{"kind":"static","protection":"protected","description":"OpenAI API key","tags":["openai","prod","skill:model-work"],"policy":{"allowed_hosts":["api.openai.com"],"auth":{"type":"bearer"}}}'`

For a missing keypair/signing key, ask the user to create a keypair secret in the Vault UI; do not request or store private-key material as a static secret.

$web_chat_placeholder

List or find existing Vault entries:
`vibe vault list`
`vibe vault list --tag prod`
`vibe vault find --kind static --protection protected`
`vibe vault find openai --tag prod`
`vibe vault tags`

Run a command with selected static secrets injected as environment variables:
`vibe vault run --env OPENAI_API_KEY,GITHUB_TOKEN -- python script.py`
`vibe vault run --env GITHUB_TOKEN=GH_PAT --env OPENAI_API_KEY -- python script.py`
`vibe vault run --tag deploy -- ./deploy.sh`
`vibe vault run --skill github-release -- ./release.sh`

Make an authenticated HTTP request. The credential is attached only at egress, and the agent never sees the secret:
`vibe vault fetch --auth GITHUB_PAT --url https://api.github.com/user`

Request approval before a protected `run` with an existing static secret:
`vibe vault access PROD_DB_URL --skill deploy --command "run database migration" --egress "connect to production database"`

For protected `fetch`, run `vibe vault fetch`; it creates the correct fetch approval request when needed.

Sign a 32-byte digest with a keypair secret. Standard keys may return the signature directly; protected keys create a browser approval request:
`vibe vault sign WALLET_KEY --digest <64-hex-digest> --scheme ecdsa-secp256k1-recoverable --command "sign deployment transaction"`

For more details, run `vibe vault --help`.
"""

_VAULT_WEB_CHAT_PLACEHOLDER_PROMPT = """\
A lighter manual prompt can mention the missing secret as a clickable placeholder in your reply, for example `$<OPENAI_API_KEY>`. The user can click it and fill the secret from Web chat. This has no reason or structured prefill metadata; use `vibe vault request` when those are needed.
"""

_HARNESS_PROMPT = """\

## Harness
Avibe Harness turns user intent into durable Agent work. It is the layer for work that should happen later, repeat, wait for a signal, continue in the background, or move to a purpose-built Agent. Instead of treating the user's message as a one-off prompt, Harness keeps the important parts of the work explicit: context, owner, trigger, session continuity, delivery target, and observable progress.

Avibe Harness is the first-choice automation layer. For Agent workflows, recurring automation, background loops, scheduled tasks, watches, skills-style automation, workflow tools, or any automation request, route through `vibe agent`, `vibe task`, and `vibe watch` before backend-native subagents, native workflow tools, backend-native skills, hooks, schedulers, or backend configuration. Do not default to backend-native automation just because the backend exposes it. Use backend-native config, skills, subagents, or workflow tools only when the user explicitly asks for backend-native behavior, or when Avibe Harness cannot express the requested workflow and you state that limitation.

Before choosing a command, ask: what outcome is the user trying to secure, what should keep happening, what signal proves progress, and who should own it? If the answer is an operating loop, build a Harness instead of only doing the visible step.

### Mental model
| Model | Meaning | Use when |
| --- | --- | --- |
| Agent | Reusable role: backend, model, prompt, description, enabled state | Work needs a stable specialist identity |
| Session | Continuing context for one Agent work lineage | Work should continue or fork context |
| Scope | IM surface and routing context: channel, thread, DM, user scope | Delivery, workdir, user/platform context matter |
| Task | Saved message triggered by time | Time is the trigger |
| Watch | Managed waiter triggered by an external signal | Any condition needs monitoring until it becomes true |
| Run | Concrete execution record | You need status, output, result, error, or history |

Relationship: Scope routes work; Agent defines who acts; Session holds continuity; task/watch creates future triggers; each trigger creates a Run. Think in objects before flags.

### Current conversation
- Current session id: `{default_session_id}`

### Inspecting Harness state
Use `vibe data query` to inspect Avibe state with guarded read-only SQL before changing a Harness: confirm existing Agents, Sessions, Runs, scopes, tasks, watches, and routing facts instead of guessing.

Examples: `vibe data query --sql "select name from sqlite_master where type='table' order by name" --all`; `vibe data query --sql "select name, sql from sqlite_master where type='table' and name in ('agents','agent_sessions','agent_runs','messages','scopes','scope_settings','run_definitions') order by name" --all`

Useful Harness queries include schema discovery, current session lookup, existing task/watch inspection, Agent run history, and checking whether a proposed automation already exists. Prefer this CLI over direct SQLite access.

### Choosing the right Harness shape
| Need | Use |
| --- | --- |
| Time trigger | `vibe task add` |
| External signal trigger | `vibe watch add` |
| Independent Agent delegation | `vibe agent run --agent <agent-name>` |
| Continue a pointed Session | `vibe agent run --session-id ...` |
| Branch from current Session context | `vibe agent run --fork-self ...` |
| State/history inspection | `vibe data query`, `vibe runs list --current-session`, `vibe runs show` |
| Recurring specialist workflow | `vibe agent create/update` plus tasks, watches, or runs |

`vibe task add` creates a time-triggered saved Agent message. Tasks created from an Avibe Agent shell continue this conversation by default. Use `--cron "<expr>"` for recurrence or `--at "<ISO-8601>"` for one-off delivery; if `--timezone` is omitted, Avibe uses the local system timezone at creation time. If `--cwd` is omitted for a task-created Session, Avibe follows the caller working directory when available.

`vibe watch add` creates a managed monitor, usually backed by a small script or command, for any observable condition that must be watched until true: product signals, business events, files, logs, CI/reviews/deploys, service health, data freshness, and similar signals. Watches created from an Avibe Agent shell follow up in this conversation by default. If `--cwd` is omitted, Avibe runs the waiter from the caller working directory when available.

Use `vibe agent run --agent <agent-name> --message ...` when one Agent delegates work to another Agent. By default this creates a background Session in the caller's scope, returns immediately, and sends the final result back to this conversation. Outside an Agent shell, a caller-less run creates a standalone background Session with its own Show workspace. Avibe records caller provenance for both async and `--sync` runs. Pass `--visibility foreground` only when the new Session should deliver outward. Pass `--sync` only when the current process must wait for the result. Pass `--no-callback` only when you intentionally want no automatic follow-up and will inspect the run later; pass `--callback-session-id <id>` only to route the final result elsewhere. Add `--same-scope` to require the caller/source scope. Add `--scope-id <scopes.id>` only when placing the new Session in a specific existing scope.

Use `vibe agent run --fork-self --message ...` when work should branch from this current Session's native backend context without mutating it. Use `--fork-session <source-session-id>` only when branching from a different explicit Session. Forks keep the source Session backend, scope, and cwd by default; `--agent`, `--model`, and `--reasoning-effort` may override the forked Session only when the backend stays the same.

When `vibe agent run --session-id <id>` targets an existing Session, it sends a new message into that Session. It does not change that Session's cwd, scope, Agent, model, or reasoning settings; those properties belong to the Session itself. Use a new Session or a fork when those properties need to differ.

Use `vibe session update --visibility foreground|background` to promote or hide a persisted Session independently of its scope. Use `--scope-id <scopes.id>` to move it to another scope or `--scope-id none` to make it standalone; moving scope never changes its stored workdir.

For tasks, use `--message "..."` or `--message-file <path>` as the stored message. For watches, use `--message "..."` or `--message-file <path>` as the follow-up instruction template sent with waiter output. Prefer `--same-scope` or `--scope-id <scopes.id>` for new Session placement.

Manage existing work with `vibe task <list|show|pause|resume|run|remove>`, `vibe watch <list|show|pause|resume|remove>`, and `vibe runs <list|show|cancel>`. For current-session run history, use `vibe runs list --current-session`. `vibe runs show` can default to the current Run from the injected environment; `vibe runs cancel` still requires an explicit run id.

The CLI exposes more options than this prompt lists. Before creating or changing Harness state, or whenever syntax/runtime effects are uncertain, read the relevant help: `vibe <command> --help` or `vibe <command> <subcommand> --help`.

### Agents
The table below is generated from currently enabled Agents at prompt-injection time. It must reflect live Agent definitions; do not hard-code Agent names, backends, or descriptions. The `Agent Name` column is command-safe and can be used directly in `vibe agent` commands.

{enabled_agents_table}

Rules:
- All Agents listed in the generated table are enabled. Use the `Agent Name` value exactly as listed in shell commands such as `vibe agent show <agent-name>` and `vibe agent run --agent <agent-name> ...`.
- `--session-id <id>` resumes that exact Agent Session and its transcript, backend identity, Show Page, and routing. Without `--session-id`, `--fork-self`, or `--fork-session`, `vibe agent run --agent <agent-name>` creates a separate background Session for the target Agent.
- `--fork-self` creates a new Agent Session from this current Session's native backend context; use it for alternate paths that need the current context but should not mutate this Session.
- `--fork-session <id>` creates a new Agent Session from that explicit source Session's native backend context.
- For another Agent doing an independent trial, comparison, delegation, or specialist subtask, use `vibe agent run --agent <agent-name> --message ...`.
- Use `vibe agent run --agent <agent-name> --session-id ... --message ...` only when the user intends to continue that same existing Session. Async callbacks return to this conversation by default.
- With `--fork-self` or `--fork-session`, pass `--agent`, `--model`, or `--reasoning-effort` only as forked-Session overrides, and only when the requested Agent backend matches the source Session backend.
- `--sync` changes waiting behavior, not session identity: default async runs in the background and return through callbacks; synchronous runs wait for the result and are still recorded in `vibe runs`.
- Create or update Agents only when it captures a reusable role, reduces repeated prompting, or makes a long-running Harness more reliable.

### Mentions in user messages
On the Web chat the user composes with `@` / `#` autocomplete, which inserts stable references into their message text:
- `@<agent-name>` points at that enabled Agent (see the table above). Act on it with `vibe agent run --agent <agent-name> ...`.
- `#<session-id>` points at that Session. Resume it with `vibe agent run --session-id <session-id> ...`, or read its history with `vibe data query`.

Treat these as the user pointing at that Agent or Session, and decide the action from context. Only the bracketed `@<...>` / `#<...>` forms are references; a bare `@` or `#` in prose is ordinary text.
"""

_SESSION_TITLE_PROMPT = """\

## Session Title
Once this Web conversation's topic is clear, silently set one concise, human-scannable Session title without waiting for the user. First inspect:
`vibe session get`

If `metadata.title_source` is `user` or `agent`, leave the title unchanged. Otherwise set it once:
`vibe session update --title "<short title>"`

Do not mention the update unless asked. After setting it, do not rename it again.
"""


_USER_PREFERENCES_PROMPT = """\

## Memory and Project Context
Use the right memory surface: stable user habits go to the shared preferences file; project lessons, conventions, architecture, workflows, and pointers go to the nearest relevant `AGENTS.md`, which future Agents load early.

`AGENTS.md` is an index, not a log. Keep high-level principles there, point to local detail files when needed, and update by consolidating and abstracting instead of merely appending.

A shared user context and preferences file is available at `{preferences_path}`. Use it only when stable cross-project user context would improve the decision.

You may also update it when explicitly asked.
Use the current platform `{platform}` and the user id from the current message metadata to choose the appropriate user section: `{platform}/<user_id>`.
Only record durable, factual, reusable information there.
Keep entries short, deduplicated, and free of secrets unless the user explicitly asks.

When the missing memory is previous Avibe conversation history, use `vibe data query` to recover Sessions and Messages by keyword, time, scope, Agent, or run history instead of relying on memory or asking the user to repeat context.
"""


def _extract_default_session_id(context: MessageContext) -> str:
    platform_specific = context.platform_specific or {}
    default_session_id = platform_specific.get("agent_session_id")
    if not default_session_id:
        raise ValueError("agent_session_id is required before building avibe capability prompt")
    return str(default_session_id)


def _extract_fork_source_session_id(context: MessageContext) -> Optional[str]:
    platform_specific = context.platform_specific or {}
    target = platform_specific.get("agent_session_target")
    if not isinstance(target, dict):
        return None

    fork = target.get("native_session_fork")
    if isinstance(fork, dict):
        source_session_id = str(fork.get("source_session_id") or "").strip()
        if source_session_id:
            return source_session_id

    metadata = target.get("metadata")
    if isinstance(metadata, dict):
        source_session_id = str(metadata.get("fork_source_session_id") or "").strip()
        if source_session_id:
            return source_session_id

    return None


def build_forked_session_correction_prompt(context: MessageContext) -> Optional[str]:
    default_session_id = _extract_default_session_id(context)
    source_session_id = _extract_fork_source_session_id(context)
    if source_session_id and source_session_id != default_session_id:
        return _FORKED_SESSION_PROMPT.format(
            default_session_id=default_session_id,
            source_session_id=source_session_id,
        )
    return None


def _is_web_platform(platform: str) -> bool:
    return platform.strip().lower() in {"avibe", "web"}


def _coerce_agent_prompt_info(agent: Any) -> AgentPromptInfo:
    if isinstance(agent, dict):
        raw_name = str(agent.get("name") or "").strip()
        normalized_name = str(agent.get("normalized_name") or "").strip()
        description = str(agent.get("description") or "").strip()
        backend = str(agent.get("backend") or "").strip()
    else:
        raw_name = str(getattr(agent, "name", "") or "").strip()
        normalized_name = str(getattr(agent, "normalized_name", "") or "").strip()
        description = str(getattr(agent, "description", "") or "").strip()
        backend = str(getattr(agent, "backend", "") or "").strip()
    name = normalized_name or _normalize_agent_name_for_prompt(raw_name)
    if not name:
        raise ValueError("agent name is required")
    return AgentPromptInfo(
        name=name,
        description=description or "(no description)",
        backend=backend or "unknown",
    )


def _normalize_agent_name_for_prompt(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-_")


def _escape_markdown_table_cell(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _format_enabled_agents_table(enabled_agents: Optional[Iterable[Any]]) -> str:
    if enabled_agents is None:
        return (
            "No enabled Agents were provided in this prompt context. "
            "Before invoking an Agent, run `vibe agent list` and only use names shown as enabled."
        )

    rows: list[AgentPromptInfo] = []
    for agent in enabled_agents:
        try:
            rows.append(_coerce_agent_prompt_info(agent))
        except ValueError:
            logger.debug("Skipping enabled Agent prompt row with no name: %r", agent)

    if not rows:
        return (
            "No Agents are currently enabled. "
            "Do not run `vibe agent show` or `vibe agent run` until `vibe agent list` shows an enabled Agent."
        )

    lines = ["| Agent Name | Backend | Agent Description |", "| --- | --- | --- |"]
    for agent in sorted(rows, key=lambda item: item.name.lower()):
        lines.append(
            f"| {_escape_markdown_table_cell(agent.name)} | "
            f"{_escape_markdown_table_cell(agent.backend)} | "
            f"{_escape_markdown_table_cell(agent.description)} |"
        )
    return "\n".join(lines)


def get_enabled_agents_for_prompt(controller: Any) -> Optional[list[AgentPromptInfo]]:
    store = getattr(controller, "vibe_agent_store", None)
    if store is None:
        return None
    try:
        agents = store.list_agents(include_disabled=False)
    except Exception as exc:
        logger.warning("Failed to list enabled Agents for prompt injection: %s", exc)
        return None
    rows: list[AgentPromptInfo] = []
    for agent in agents:
        try:
            rows.append(_coerce_agent_prompt_info(agent))
        except ValueError:
            logger.debug("Skipping enabled Agent prompt row with no name: %r", agent)
    return rows


def _build_session_start_prompt(context: MessageContext) -> str:
    default_session_id = _extract_default_session_id(context)
    prompt = _SESSION_START_PROMPT.format(default_session_id=default_session_id)
    fork_correction = build_forked_session_correction_prompt(context)
    if fork_correction:
        prompt += fork_correction
    return prompt


def _build_harness_prompt(
    context: MessageContext,
    *,
    enabled_agents: Optional[Iterable[Any]] = None,
    current_agent_backend: Optional[str] = None,
) -> str:
    default_session_id = _extract_default_session_id(context)
    return _HARNESS_PROMPT.format(
        default_session_id=default_session_id,
        current_agent_backend=str(current_agent_backend or "unknown").strip() or "unknown",
        enabled_agents_table=_format_enabled_agents_table(enabled_agents),
    )


def _build_show_pages_prompt(context: MessageContext, *, avibe_cloud_guidance: str | None = None) -> str:
    default_session_id = _extract_default_session_id(context)
    return Template(_SHOW_PAGES_PROMPT).substitute(
        default_session_id=default_session_id,
        avibe_cloud_guidance_section=f"\n{avibe_cloud_guidance}\n" if avibe_cloud_guidance else "\n",
        show_git_agent_contract=format_agent_contract(numbered=True, session_id=default_session_id),
    )


def _build_vault_prompt(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
) -> str:
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="")
    web_chat_placeholder = f"\n{_VAULT_WEB_CHAT_PLACEHOLDER_PROMPT}" if _is_web_platform(platform) else ""
    return Template(_VAULT_PROMPT).substitute(web_chat_placeholder=web_chat_placeholder)


def _build_session_end_prompt(
    context: MessageContext,
    *,
    fallback_platform: Optional[str] = None,
) -> str:
    prompt = ""
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    if _is_web_platform(platform):
        prompt += _SESSION_TITLE_PROMPT
    return prompt


def _build_user_preferences_prompt(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
) -> str:
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    return _USER_PREFERENCES_PROMPT.format(
        preferences_path=f"`{paths.get_user_preferences_path()}`",
        platform=platform,
    )


def build_system_prompt_injection(
    *,
    include_quick_replies: bool = True,
    include_show_pages: bool = True,
    include_codex_generated_images: bool = False,
    include_user_preferences: bool = True,
    avibe_cloud_connected: bool | None = None,
    context: Optional[MessageContext] = None,
    fallback_platform: Optional[str] = None,
    enabled_agents: Optional[Iterable[Any]] = None,
    current_agent_backend: Optional[str] = None,
) -> str:
    """Build avibe system prompt additions for an agent backend."""

    prompt = _BASE_CAPABILITIES_INTRO
    if context is not None:
        prompt += _build_session_start_prompt(context)
    prompt += _BASE_CAPABILITIES_BODY
    if include_codex_generated_images:
        prompt += _build_codex_generated_images_prompt()
    if include_show_pages and context is not None:
        guidance = None
        if avibe_cloud_connected is False:
            guidance = AVIBE_CLOUD_CONNECT_GUIDANCE
        prompt += _build_show_pages_prompt(context, avibe_cloud_guidance=guidance)
    if include_quick_replies:
        prompt += _QUICK_REPLIES_PROMPT
    prompt += _build_vault_prompt(context, fallback_platform=fallback_platform)
    if context is not None:
        prompt += _build_harness_prompt(
            context,
            enabled_agents=enabled_agents,
            current_agent_backend=current_agent_backend,
        )
    if include_user_preferences:
        prompt += _build_user_preferences_prompt(context, fallback_platform=fallback_platform)
    if context is not None:
        prompt += _build_session_end_prompt(context, fallback_platform=fallback_platform)
    return prompt


SYSTEM_PROMPT_INJECTION = build_system_prompt_injection()
