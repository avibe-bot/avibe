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
from core.prompt_registry import prompt_text, render_prompt
from core.show_git import format_agent_contract
from modules.im import MessageContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentPromptInfo:
    name: str
    description: str
    backend: str = "unknown"


_BASE_CAPABILITIES_INTRO = prompt_text("base-capabilities-intro")

_BASE_CAPABILITIES_BODY = prompt_text("base-capabilities-body")

_SHOW_PAGES_PROMPT = prompt_text("show-pages-prompt")

_VAULT_ROUTING_PROMPT = prompt_text("vault-routing-prompt")

_HARNESS_ROUTING_PROMPT = prompt_text("harness-routing-prompt")

_HARNESS_AGENTS_PROMPT = prompt_text("harness-agents-prompt")

_SESSION_START_PROMPT = prompt_text("session-start-prompt")

_FORKED_SESSION_PROMPT = prompt_text("forked-session-prompt")


def _build_codex_generated_images_prompt() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    example_uri = (codex_home / "generated_images" / "thread-id" / "image-file.png").as_uri()
    return render_prompt("codex-generated-images", example_uri=example_uri)


_QUICK_REPLIES_PROMPT = prompt_text("quick-replies-prompt")

_SESSION_TITLE_PROMPT = prompt_text("session-title-prompt")

_PREFERENCES_CONTEXT_PROMPT = prompt_text("preferences-context-prompt")

_MEMORY_CONTEXT_PROMPT = prompt_text("memory-context-prompt")


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


def _format_enabled_agents_table(enabled_agents: Optional[Iterable[Any]]) -> str:
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

    lines = ["| Agent Name | Backend | Agent Description |", "| --- | --- | --- |"]
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


def _build_session_start_prompt(context: MessageContext) -> str:
    default_session_id = _extract_default_session_id(context)
    prompt = render_prompt("session-start-prompt", default_session_id=default_session_id)
    fork_correction = build_forked_session_correction_prompt(context)
    if fork_correction:
        prompt += fork_correction
    return prompt


def _build_harness_prompt(
    *,
    enabled_agents: Optional[Iterable[Any]] = None,
) -> str:
    prompt = _HARNESS_ROUTING_PROMPT
    table = _format_enabled_agents_table(enabled_agents)
    if table:
        prompt += render_prompt("harness-agents-prompt", enabled_agents_table=table)
    return prompt


def _build_show_pages_prompt(
    context: MessageContext,
) -> str:
    default_session_id = _extract_default_session_id(context)
    prompt = _SHOW_PAGES_PROMPT
    history_contract = format_agent_contract(numbered=True, session_id=default_session_id)
    if history_contract:
        prompt += f"\nHistory contract:\n{history_contract}\n"
    return prompt


def _build_vault_prompt(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
) -> str:
    del context, fallback_platform
    return _VAULT_ROUTING_PROMPT


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


def _build_context_prompt(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
    memory_enabled: bool = False,
) -> str:
    if memory_enabled:
        return _MEMORY_CONTEXT_PROMPT
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    return render_prompt(
        "preferences-context-prompt",
        preferences_path=f"`{paths.get_user_preferences_path()}`",
        platform=platform,
    )


def build_system_prompt_injection(
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
) -> str:
    """Build avibe system prompt additions for an agent backend."""

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

    prompt = _BASE_CAPABILITIES_INTRO
    if context is not None:
        prompt += _build_session_start_prompt(context)
    prompt += _BASE_CAPABILITIES_BODY
    if include_codex_generated_images:
        prompt += _build_codex_generated_images_prompt()
    if include_show_pages and context is not None:
        prompt += _build_show_pages_prompt(context)
    if include_quick_replies:
        prompt += _QUICK_REPLIES_PROMPT
    if vault_skill_available:
        prompt += _build_vault_prompt(context, fallback_platform=fallback_platform)
    if context is not None:
        prompt += _build_harness_prompt(
            enabled_agents=enabled_agents,
        )
    if include_context_guidance:
        prompt += _build_context_prompt(
            context,
            fallback_platform=fallback_platform,
            memory_enabled=memory_enabled,
        )
    if skills is not None:
        from core.managed_skills import render_skill_catalog_prompt

        prompt += render_skill_catalog_prompt(skills)
    if context is not None:
        prompt += _build_session_end_prompt(context, fallback_platform=fallback_platform)
    return prompt
