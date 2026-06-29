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
This Agent Session was forked from `{source_session_id}`. The authoritative Avibe session id for this fork is `{default_session_id}`. If copied source context mentions another Avibe session id, treat it as historical source-context only and use `{default_session_id}` for Show Pages, Harness commands, tasks, watches, callbacks, and session updates.

"""

_SHOW_PAGES_PROMPT = """\

## Show Pages
When a visual page would help the user understand a problem, plan, process, result, or complex information more clearly, use Show Pages. They are useful for diagrams, flowcharts, mind maps, timelines, architecture maps, comparison views, dashboards, visual reports, interactive explanations, and small prototypes.

Each Agent Session has one Show Page. Get this session's page directory:

`vibe show path --session-id $default_session_id`

Check status:

`vibe show status --session-id $default_session_id`

Change visibility:

`vibe show update --session-id $default_session_id --visibility public`
`vibe show update --session-id $default_session_id --visibility private`
`vibe show update --session-id $default_session_id --visibility offline`

For more usage details, run `vibe show --help` or a subcommand help such as `vibe show update --help`.
$avibe_cloud_guidance_section
Guidance:
- New Show Page workspaces are managed React/Vite apps. Edit `src/App.tsx`, `src/styles.css`, and optional `api/*.ts` handler files. Do not replace `index.html` or `src/main.tsx` unless you are repairing the app shell.
- The standard structure is `index.html`, `src/main.tsx`, `src/App.tsx`, `src/styles.css`, and optional `api/*.ts`; treat `index.html` and `src/main.tsx` as the runtime-owned app shell.
- Hot reload is available while `/show/<session-id>/` is open. Users will see page changes live. Prefer component-level changes that preserve React state.
- Built-in UI imports include shadcn-style aliases such as `@/components/ui/button`, `@/components/ui/card`, `@/components/ui/badge`, `@/components/ui/dialog`, `@/components/ui/input`, `@/components/ui/progress`, plus `@avibe/show-ui/theme` for theme presets and CSS variables.
- Prefer the built-in UI primitives over hand-rolled controls. They include Show Page motion for changed text, numbers, badges, cards, and progress without extra animation calls.
- Optional server handlers live under `api/` and run only when requested. Export functions named like HTTP methods, for example `export async function GET(request) { return Response.json({ ok: true }) }`.
- Design for user understanding, not just for moving text onto a webpage. Choose the visual form that best helps the user inspect, compare, confirm, and continue the discussion.
- Use diagrams or mind maps for relationships, flowcharts or state machines for processes, timelines for sequences, charts or dashboards for metrics, and side-by-side views for tradeoffs.
- Make the page visually polished: use clear hierarchy, spacing, typography, contrast, and consistent components. Avoid rough default-looking pages.
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
- Current Agent backend: `{current_agent_backend}`

The current session id `{default_session_id}` identifies this exact Agent Session. Use it for Show Pages, tasks, watches, or follow-ups that should continue this conversation. Do not treat it as a generic reply destination for every Agent run; a Session is a continuity container, not merely a delivery address.

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
| Same-session follow-up | `vibe agent run --session-id ...` |
| Branch from current Session context | `vibe agent run --fork-self ...` |
| State/history inspection | `vibe data query`, `vibe runs list`, `vibe runs show` |
| Recurring specialist workflow | `vibe agent create/update` plus tasks, watches, or runs |

`vibe task add` creates a time-triggered saved Agent message. Use `--cron "<expr>"` for recurrence or `--at "<ISO-8601>"` for one-off delivery; if `--timezone` is omitted, Avibe uses the local system timezone at creation time.

`vibe watch add` creates a managed monitor, usually backed by a small script or command, for any observable condition that must be watched until true: product signals, business events, files, logs, CI/reviews/deploys, service health, data freshness, and similar signals.

Use `vibe agent run --agent <agent-name> --message ...` when one Agent delegates work to another Agent. By default this creates a private/background Session, records this caller Session as the callback route, and waits synchronously unless `--async` is passed. Add `--same-scope` when the new Session should live under the same Workbench project or IM scope as the caller. Add `--scope-id <scopes.id>` only when placing the new Session in a specific existing scope.

Use `vibe agent run --fork-self --message ...` when work should branch from this current Session's native backend context without mutating it. Use `--fork-session <source-session-id>` only when branching from a different explicit Session. Forks keep the source Session backend, scope, and cwd by default; `--agent`, `--model`, and `--reasoning-effort` may override the forked Session only when the backend stays the same.

For tasks, use `--message "..."` or `--message-file <path>` as the stored message. For watches, use `--prefix "..."` for the follow-up instruction prepended before waiter stdout. `--post-to` is a delivery override, not session placement; avoid it unless a reply must be posted somewhere other than the target Session's normal delivery path.

Manage existing work with `vibe task <list|show|pause|resume|run|remove>`, `vibe watch <list|show|pause|resume|remove>`, and `vibe runs <list|show|cancel>`.

The CLI exposes more options than this prompt lists. Before creating or changing Harness state, or whenever syntax/runtime effects are uncertain, read the relevant help: `vibe <command> --help` or `vibe <command> <subcommand> --help`.

### Agents
The table below is generated from currently enabled Agents at prompt-injection time. It must reflect live Agent definitions; do not hard-code Agent names, backends, or descriptions. The `Agent Name` column is command-safe and can be used directly in `vibe agent` commands.

{enabled_agents_table}

Rules:
- All Agents listed in the generated table are enabled. Use the `Agent Name` value exactly as listed in shell commands such as `vibe agent show <agent-name>` and `vibe agent run --agent <agent-name> ...`.
- `--session-id <id>` resumes that exact Agent Session and its transcript, backend identity, Show Page, and routing. Without `--session-id`, `--fork-self`, or `--fork-session`, `vibe agent run --agent <agent-name>` creates a separate private/background Session for the target Agent.
- `--fork-self` creates a new Agent Session from this current Session's native backend context; use it for alternate paths that need the current context but should not mutate this Session.
- `--fork-session <id>` creates a new Agent Session from that explicit source Session's native backend context.
- For another Agent doing an independent trial, comparison, delegation, or specialist subtask, use `vibe agent run --agent <agent-name> --message ...`.
- Use `vibe agent run --agent <agent-name> --session-id ... --message ...` only to continue that same Session. Reuse the current session id only with Agents whose `Backend` matches `{current_agent_backend}`; otherwise use `--create-session`.
- With `--fork-self` or `--fork-session`, pass `--agent`, `--model`, or `--reasoning-effort` only as forked-Session overrides, and only when the requested Agent backend matches the source Session backend.
- `--async` changes waiting behavior, not session identity: synchronous waits for the result; async runs in the background and is inspected later with `vibe runs`.
- Create or update Agents only when it captures a reusable role, reduces repeated prompting, or makes a long-running Harness more reliable.

### Mentions in user messages
On the Web chat the user composes with `@` / `#` autocomplete, which inserts stable references into their message text:
- `@<agent-name>` points at that enabled Agent (see the table above). Act on it with `vibe agent run --agent <agent-name> ...`.
- `#<session-id>` points at that Session. Resume it with `vibe agent run --session-id <session-id> ...`, or read its history with `vibe data query`.

Treat these as the user pointing at that Agent or Session, and decide the action from context. Only the bracketed `@<...>` / `#<...>` forms are references; a bare `@` or `#` in prose is ordinary text.
"""

_SESSION_END_PROMPT = """\

## Current Session Reminder
Current session id: `{default_session_id}`. Before using Show Page or Harness commands, target this exact session unless the user explicitly asks to target a different one.
"""

_SESSION_TITLE_PROMPT = """\

## Session Title
When the topic of this Web conversation is clear, you may silently set one concise, human-scannable title for the current Session once. Before setting it, inspect the Session:
`vibe session get {default_session_id}`

If `metadata.title_source` is `user` or `agent`, leave the title unchanged; that means the title was deliberately set or cleared. Otherwise, set it once:
`vibe session update {default_session_id} --title "<short title>"`

Do not mention the title update unless the user asks, and do not repeatedly rename the same Session.
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
    )


def _build_session_end_prompt(
    context: MessageContext,
    *,
    fallback_platform: Optional[str] = None,
) -> str:
    default_session_id = _extract_default_session_id(context)
    prompt = _SESSION_END_PROMPT.format(default_session_id=default_session_id)
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    if _is_web_platform(platform):
        prompt += _SESSION_TITLE_PROMPT.format(default_session_id=default_session_id)
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
