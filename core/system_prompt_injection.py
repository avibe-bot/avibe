"""System prompt injection helpers for avibe agent backends."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from config import paths
from core.message_context import resolve_context_platform
from core.prompt_registry import RenderedPromptBlock, join_prompt_blocks, render_prompt, render_prompt_block
from core.show_git import agent_contract_block
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
# - Working principles are a static, unconditional prefix before Session and
#   capability details; never gate them on a backend, Skill, or Turn.
# - Author all injected prose in the registry. Production text and debug JSON
#   must consume the same ordered rendered blocks; Studio never assembles prose.


@dataclass(frozen=True)
class AgentPromptInfo:
    name: str
    description: str
    backend: str = "unknown"


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


def _format_enabled_agents_rows(enabled_agents: Optional[Iterable[Any]]) -> str:
    if enabled_agents is None:
        return ""

    rows: list[AgentPromptInfo] = []
    for agent in enabled_agents:
        try:
            rows.append(_coerce_agent_prompt_info(agent))
        except ValueError:
            logger.debug("Skipping enabled Agent prompt row with no name: %r", agent)

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


def _context_block(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
    memory_enabled: bool = False,
) -> RenderedPromptBlock:
    if memory_enabled:
        return render_prompt_block("memory-context-prompt")
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    return render_prompt_block(
        "preferences-context-prompt",
        preferences_path=f"`{paths.get_user_preferences_path()}`",
        platform=platform,
    )


def build_system_prompt_blocks(
    *,
    include_quick_replies: bool = True,
    include_show_pages: bool = True,
    include_codex_generated_images: bool = False,
    include_context_guidance: bool = True,
    memory_enabled: bool = False,
    context: Optional[MessageContext] = None,
    fallback_platform: Optional[str] = None,
    enabled_agents: Optional[Iterable[Any]] = None,
    skills_cwd: str | Path | None = None,
    skills_project_base: str | Path | None = None,
    skills_claude_cli_path: str | None = None,
) -> list[RenderedPromptBlock]:
    """The production composition, also exported by the debug command."""

    skills = None
    if skills_cwd is not None:
        from core.managed_skills import resolve_skills

        skills = resolve_skills(
            skills_cwd,
            project_base=skills_project_base,
            claude_cli_path=skills_claude_cli_path,
        )
        if not include_show_pages:
            skills = [skill for skill in skills if skill.name != "use-show-pages"]

    advertisable_skills = [] if skills is None else [skill for skill in skills if not skill.disable_model_invocation]
    vault_skill_available = any(
        skill.name == "use-avibe-vault" for skill in advertisable_skills
    )

    blocks = [
        render_prompt_block("base-capabilities-intro"),
        render_prompt_block("agent-working-principles"),
    ]
    if context is not None:
        blocks.append(render_prompt_block("session-start-prompt", default_session_id=_extract_default_session_id(context)))
        correction = build_forked_session_correction_prompt(context)
        if correction:
            blocks.append(RenderedPromptBlock("forked-session-prompt", correction))
    blocks.append(render_prompt_block("base-capabilities-body"))
    if include_codex_generated_images:
        blocks.append(_codex_generated_images_block())
    if include_show_pages and context is not None:
        blocks.append(render_prompt_block("show-pages-prompt"))
        history = agent_contract_block(numbered=True, session_id=_extract_default_session_id(context))
        if history:
            blocks.append(render_prompt_block("show-history-heading"))
            blocks.append(RenderedPromptBlock(history.module_id, history.text + "\n"))
    if include_quick_replies:
        blocks.append(render_prompt_block("quick-replies-prompt"))
    if vault_skill_available:
        blocks.append(render_prompt_block("vault-routing-prompt"))
    if context is not None:
        blocks.append(render_prompt_block("harness-routing-prompt"))
        agent_rows = _format_enabled_agents_rows(enabled_agents)
        if agent_rows:
            blocks.append(render_prompt_block("harness-agents-prompt", enabled_agents_rows=agent_rows))
    if include_context_guidance:
        blocks.append(_context_block(
            context,
            fallback_platform=fallback_platform,
            memory_enabled=memory_enabled,
        ))
    if skills is not None:
        from core.managed_skills import render_skill_catalog_blocks

        blocks.extend(render_skill_catalog_blocks(skills))
    if context is not None:
        platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
        if _is_web_platform(platform):
            blocks.append(render_prompt_block("session-title-prompt"))
    return blocks


def build_system_prompt_injection(**kwargs: Any) -> str:
    """Render the production blocks without changing their byte boundaries."""
    return join_prompt_blocks(build_system_prompt_blocks(**kwargs))
