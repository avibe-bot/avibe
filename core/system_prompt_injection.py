"""System prompt injection helpers for avibe agent backends."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from core.message_context import resolve_context_platform
from core.prompt_registry import RenderedPromptBlock, join_prompt_blocks, order_prompt_blocks, render_prompt, render_prompt_block
from modules.im import MessageContext

logger = logging.getLogger(__name__)

# System Prompt rendering invariants:
# - Preserve backend prompt caches: rendered bytes change only when authored
#   content, stable configuration, or intentionally live catalogs change.
# - Compose complete, positive capability modules. Omit unavailable capabilities;
#   keep recovery, compatibility, and operational detail in routed Skills.
# - Never branch Prompt content on turn-scoped authorization or incidental runtime
#   health. Runtime policy belongs in the enforcing layer, not in its description.
# - Keep every generated collection deterministic, including Agent and Skill order.
# - Required built-in Skill routing is unconditional; installation guarantees it.
# - Show Page history rules live in its Skill; inspect workspace ownership only
#   on demand, never while composing the conversation's stable prompt.
# - Working principles are a static, unconditional prefix before Session and
#   capability details; never gate them on a backend, Skill, or Turn.
# - Model-family guidance follows the principles and is gated only on the Turn's
#   resolved model, which is stable Session configuration.
# - Author all injected prose in the registry. Production text and debug JSON
#   must consume the same ordered rendered blocks; Studio never assembles prose.
# - The registry owns composition order. Keep static Skill usage guidance after
#   the base capabilities and dynamic Skill rows near the end to protect caches.
# - Agent-authored instructions are the final content block, after all Avibe
#   guidance and catalogs; backends must not prepend or append their own copy.


#: The main Bot's name in prompts while the user has not named it (``docs/plans/bots.md`` D5).
MAIN_BOT_DEFAULT_DISPLAY_NAME = "Vibey"


@dataclass(frozen=True)
class AgentPromptInfo:
    name: str
    description: str
    backend: str = "unknown"
    #: Set when the Agent is a Bot: the Bots table lists it, and the Agents table leaves it out.
    bot_handle: str = ""
    bot_display_name: str = ""
    bot_builtin: bool = False


@dataclass(frozen=True)
class BotPromptInfo:
    """The Bot a Vibey Turn runs as, for its own sections (``docs/plans/bots.md`` "Prompt drafts").

    Every field is a fact of the Bot, never of the Session or Turn, so a Bot's sections are byte-stable across its
    Turns and identical in its forks, which keep its directory (D7). The directory itself is not one of them: it is
    the environment block's ``cwd``.
    """

    handle: str
    display_name: str
    role: str
    session_id: str
    builtin: bool = False


def _codex_generated_images_block() -> RenderedPromptBlock:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    example_uri = (codex_home / "generated_images" / "thread-id" / "image-file.png").as_uri()
    return render_prompt_block("codex-generated-images", example_uri=example_uri)


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
        return render_prompt(
            "forked-session-prompt",
            default_session_id=default_session_id,
            source_session_id=source_session_id,
        )
    return None


def _is_web_platform(platform: str) -> bool:
    return platform.strip().lower() in {"avibe", "web"}


def _agent_bot_fields(agent: Any) -> dict[str, Any]:
    """The Bot an Agent is, from an Agent record, its list payload (``bot``), or an already coerced row."""
    if isinstance(agent, AgentPromptInfo):
        return {
            "bot_handle": agent.bot_handle,
            "bot_display_name": agent.bot_display_name,
            "bot_builtin": agent.bot_builtin,
        }
    if isinstance(agent, dict):
        bot = agent.get("bot")
        builtin = bool(bot.get("builtin")) if isinstance(bot, dict) else False
    else:
        from core.vibe_agents import agent_bot_record, is_main_bot_agent

        try:
            bot = agent_bot_record(agent) if getattr(agent, "archived_at", None) is None else None
            builtin = bool(bot) and is_main_bot_agent(agent)
        except (AttributeError, TypeError):
            bot, builtin = None, False
    if not isinstance(bot, dict) or not str(bot.get("handle") or "").strip():
        return {}
    return {
        "bot_handle": str(bot["handle"]).strip(),
        "bot_display_name": str(bot.get("display_name") or "").strip(),
        "bot_builtin": builtin,
    }


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
        **_agent_bot_fields(agent),
    )


def _normalize_agent_name_for_prompt(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-_")


def _escape_markdown_table_cell(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _coerce_enabled_agents(enabled_agents: Optional[Iterable[Any]]) -> list[AgentPromptInfo]:
    rows: list[AgentPromptInfo] = []
    for agent in enabled_agents or ():
        try:
            rows.append(_coerce_agent_prompt_info(agent))
        except ValueError:
            logger.debug("Skipping enabled Agent prompt row with no name: %r", agent)
    return rows


def _bot_display_name(name: str, *, builtin: bool) -> str:
    return name or (MAIN_BOT_DEFAULT_DISPLAY_NAME if builtin else "")


def _format_bots_rows(rows: list[AgentPromptInfo]) -> str:
    """The Bots table: the main Bot first, then by handle.

    Not the Web's order by recent use (``bots.md`` D13): this table is in every system prompt, which must not change
    when the user talks to a Bot (D11).
    """
    bots = sorted(
        (row for row in rows if row.bot_handle),
        key=lambda row: (0 if row.bot_builtin else 1, row.bot_handle),
    )
    return "\n".join(
        f"| {_escape_markdown_table_cell(bot.bot_handle)} | "
        f"{_escape_markdown_table_cell(_bot_display_name(bot.bot_display_name, builtin=bot.bot_builtin))} | "
        f"{_escape_markdown_table_cell(bot.description)} |"
        for bot in bots
    )


def _format_enabled_agents_rows(rows: list[AgentPromptInfo]) -> str:
    """The Agents table. An Agent that is a Bot is listed once, as the Bot (``bots.md`` D4)."""
    rows = [row for row in rows if not row.bot_handle]
    if not rows:
        return ""

    lines = []
    for agent in sorted(
        rows,
        key=lambda item: (
            item.name.casefold(),
            item.name,
            item.backend.casefold(),
            item.backend,
            item.description,
        ),
    ):
        lines.append(
            f"| {_escape_markdown_table_cell(agent.name)} | "
            f"{_escape_markdown_table_cell(agent.backend)} | "
            f"{_escape_markdown_table_cell(agent.description)} |"
        )
    return "\n".join(lines)


def _role_sentence(role: str) -> str:
    text = " ".join(str(role or "").split())
    if not text:
        return ""
    return f" Your role: {text}" + ("" if text[-1] in ".!?。！？" else ".")


def _bot_blocks(bot: BotPromptInfo) -> list[RenderedPromptBlock]:
    blocks = [
        render_prompt_block(
            "bot-identity-prompt",
            display_name=_bot_display_name(" ".join(bot.display_name.split()), builtin=bot.builtin) or bot.handle,
            handle=bot.handle,
            role_sentence=_role_sentence(bot.role),
            session_id=bot.session_id,
        ),
        render_prompt_block("bot-memory-prompt"),
    ]
    if bot.builtin:
        blocks.append(render_prompt_block("bot-main-prompt"))
    blocks.append(render_prompt_block("bot-harness-prompt"))
    return blocks


def bot_for_turn(store: Any, *, agent_id: Optional[str], cwd: str) -> Optional[BotPromptInfo]:
    """The Bot a Turn runs as: its Agent is the Bot's, and it runs in the Bot's directory.

    This is the one rule for the Bot sections, and it needs no Session id: the Bot's own Session runs there, and so
    does every fork of it, since forks keep the source's directory (``bots.md`` D7). A fork therefore shares its
    source's sections and its cached prefix; a fresh ``--agent`` Session of the same Agent runs elsewhere and is an
    ordinary worker. Reads the catalog: call it off the event loop.
    """
    if store is None or not agent_id or not cwd:
        return None
    from core.bots import bot_from_agent

    agent = store.get_by_id(agent_id)
    bot = bot_from_agent(agent) if agent is not None else None
    if bot is None or os.path.realpath(cwd) != os.path.realpath(bot.directory):
        return None
    return BotPromptInfo(
        handle=bot.handle,
        display_name=bot.display_name,
        role=bot.role,
        session_id=bot.session_id,
        builtin=bot.builtin,
    )


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


def get_enabled_projects_for_prompt() -> list[dict[str, str]]:
    """Read the path-free enabled Project catalog for a Bot's prompt."""
    try:
        from storage.db import create_sqlite_engine
        from storage import projects_service
        from config import paths

        engine = create_sqlite_engine(paths.get_sqlite_state_path(), read_only=True)
        try:
            with engine.connect() as conn:
                return projects_service.list_enabled_projects_for_prompt(conn)
        finally:
            engine.dispose()
    except Exception as exc:
        logger.warning("Failed to list enabled Projects for prompt injection: %s", exc)
        return []


def _project_row_text(project: Any) -> str:
    if isinstance(project, dict):
        name = project.get("name") or project.get("display_name") or ""
        description = project.get("description") or "(no description)"
    else:
        name = getattr(project, "name", "")
        description = getattr(project, "description", "") or "(no description)"
    return (
        f"| {_escape_markdown_table_cell(str(name))} | "
        f"{_escape_markdown_table_cell(str(description))} |\n"
    )


def project_catalog_page(
    projects: Iterable[Any],
    page: int = 1,
    *,
    page_size: int | None = None,
) -> tuple[Sequence[Any], int | None]:
    from core.managed_skills import CATALOG_PAGE_SIZE, catalog_page_of

    return catalog_page_of(
        list(projects),
        _project_row_text,
        page,
        page_size=CATALOG_PAGE_SIZE if page_size is None else page_size,
    )


def _format_project_rows(projects: Iterable[Any]) -> tuple[str, str]:
    rows = list(projects)
    page, next_page = project_catalog_page(rows, 1)
    more = ""
    if next_page is not None:
        more = f"\nMore Projects are available. Run `vibe project list --page {next_page}` to view more.\n"
    return "".join(_project_row_text(item) for item in page), more


def build_system_prompt_blocks(
    *,
    agent_instructions: str = "",
    backend: str = "",
    include_quick_replies: bool = True,
    include_codex_generated_images: bool = False,
    include_context_guidance: bool = True,
    context: Optional[MessageContext] = None,
    fallback_platform: Optional[str] = None,
    enabled_agents: Optional[Iterable[Any]] = None,
    skills_cwd: str | Path | None = None,
    skills_project_base: str | Path | None = None,
    skill_catalog_sink: list[dict[str, Any]] | None = None,
    bot: BotPromptInfo | None = None,
    projects: Optional[Iterable[Any]] = None,
    model: str = "",
) -> list[RenderedPromptBlock]:
    """The production composition, also exported by the debug command.

    ``bot`` is the Bot a Vibey Turn runs as (``bot_for_turn``); only then are the Bot sections rendered.
    ``model`` is the model the Turn runs on; an OpenAI GPT model also gets the GPT reporting section.
    """

    skills = None
    if skills_cwd is not None:
        from core.managed_skills import resolve_skills

        skills = resolve_skills(
            skills_cwd,
            project_base=skills_project_base,
        )

    advertisable_skills = [] if skills is None else [skill for skill in skills if not skill.disable_model_invocation]
    vault_skill_available = any(
        skill.name == "use-avibe-vault" for skill in advertisable_skills
    )

    blocks = [
        render_prompt_block("base-capabilities-intro"),
        render_prompt_block("agent-working-principles"),
    ]
    if model:
        from vibe.models_dev_catalog import model_vendor

        if model_vendor(model) == "openai":
            blocks.append(render_prompt_block("gpt-reporting-prompt"))
    if isinstance(bot, dict):
        bot = BotPromptInfo(**bot)
    if bot is not None:
        blocks.extend(_bot_blocks(bot))
    if context is not None and backend != "vibey":
        blocks.append(render_prompt_block("session-start-prompt", default_session_id=_extract_default_session_id(context)))
        correction = build_forked_session_correction_prompt(context)
        if correction:
            blocks.append(RenderedPromptBlock("forked-session-prompt", correction))
    blocks.append(render_prompt_block("base-capabilities-body"))
    if backend == "codex":
        blocks.append(render_prompt_block("codex-skill-reuse"))
    if include_codex_generated_images:
        blocks.append(_codex_generated_images_block())
    blocks.append(render_prompt_block("show-pages-prompt"))
    if include_quick_replies:
        blocks.append(render_prompt_block("quick-replies-prompt"))
    if vault_skill_available:
        blocks.append(render_prompt_block("vault-routing-prompt"))
    if context is not None:
        blocks.append(render_prompt_block("harness-routing-prompt"))
        roster = _coerce_enabled_agents(enabled_agents)
        agent_rows = _format_enabled_agents_rows(roster)
        bots_rows = _format_bots_rows(roster)
        if agent_rows or bots_rows:
            blocks.append(
                render_prompt_block("harness-agents-prompt", bots_rows=bots_rows, enabled_agents_rows=agent_rows)
            )
        if bot is not None and projects is not None:
            project_rows, more_notice = _format_project_rows(projects)
            blocks.append(render_prompt_block("projects-prompt", project_rows=project_rows, more_notice=more_notice))
    if include_context_guidance:
        blocks.append(render_prompt_block("project-context-prompt"))
    from core.mcp_hub.prompt import build_mcp_prompt_blocks

    blocks.extend(build_mcp_prompt_blocks(skills_cwd, skills_project_base))
    if skills is not None:
        from core.managed_skills import render_skill_catalog_blocks

        blocks.extend(render_skill_catalog_blocks(skills))
        if skill_catalog_sink is not None:
            try:
                from core.skill_observability import catalog_result

                skill_catalog_sink.append(catalog_result(skills, entry_point="runtime_prompt"))
            except Exception:
                logger.info("Skill catalog observation unavailable during prompt preparation")
    if context is not None and bot is None:
        # A Bot's Session is titled by the Bot's display name, so it gets no title section (bots.md D5).
        platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
        if _is_web_platform(platform):
            blocks.append(render_prompt_block("session-title-prompt"))
    if agent_instructions:
        blocks.append(render_prompt_block("agent-instructions", agent_instructions=agent_instructions))
    return order_prompt_blocks(blocks)


def build_system_prompt_injection(**kwargs: Any) -> str:
    """Render the production blocks without changing their byte boundaries."""
    return join_prompt_blocks(build_system_prompt_blocks(**kwargs))
