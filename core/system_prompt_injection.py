"""System prompt injection helpers for avibe agent backends."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from config import paths
from core.avibe_cloud import AVIBE_CLOUD_CONNECT_GUIDANCE
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

_SHOW_PAGES_SKILL_ROUTING = prompt_text("show-pages-skill-routing")

_VAULT_ROUTING_PROMPT = prompt_text("vault-routing-prompt")

_HARNESS_ROUTING_PROMPT = prompt_text("harness-routing-prompt")

_HARNESS_SKILL_ROUTING = prompt_text("harness-skill-routing")

_SESSION_START_PROMPT = prompt_text("session-start-prompt")

_FORKED_SESSION_PROMPT = prompt_text("forked-session-prompt")


def _build_codex_generated_images_prompt() -> str:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    example_uri = (codex_home / "generated_images" / "thread-id" / "image-file.png").as_uri()
    return render_prompt("codex-generated-images", example_uri=example_uri)


def memory_cli_prompt_admitted(controller: Any, context: MessageContext) -> bool:
    """Advertise scoped Memory access only on an eligible interactive turn."""

    config = getattr(controller, "config", None)
    payload = context.platform_specific if isinstance(context.platform_specific, dict) else {}
    turn_source = str(payload.get("turn_source") or "human").strip()
    admitted = bool(getattr(getattr(config, "memory", None), "enabled", False))
    admitted = admitted and turn_source == "human" and not payload.get("task_trigger_kind")
    if admitted:
        platform = resolve_context_platform(
            context,
            fallback_platform=getattr(config, "platform", None),
        )
        if platform == "avibe":
            admitted = payload.get("memory_cli_admitted") is True
        else:
            admit = getattr(controller, "memory_capture_admitted", None)
            try:
                admitted = bool(admit(context)) if callable(admit) else False
            except Exception:
                admitted = False

    configure_session = getattr(controller, "configure_memory_cli_session", None)
    if callable(configure_session):
        try:
            return bool(configure_session(context, admitted=admitted))
        except Exception:
            return False
    return admitted


_QUICK_REPLIES_PROMPT = prompt_text("quick-replies-prompt")

# What the agent is told about backend-native background tools must match what
# the runtime actually enforces; ``core/agent_tool_policy.py`` owns that
# decision and this prompt only announces it.
_TOOL_POLICY_ENFORCED_SECTION = prompt_text("tool-policy-enforced-section")

# No argument-aware hook here, so only whole tool names can be refused. Saying
# "all denied" would be wrong in both directions: it overstates what stops
# `Agent`, `ScheduleWakeup`, and `CronCreate`, and it hides that those three now
# need the agent's own judgement rather than a gate. Each is excluded from the
# name-only list because it has a legitimate non-background form no name match
# can see, which leaves its background form unguarded as well.
_TOOL_POLICY_NAME_ONLY_SECTION = prompt_text("tool-policy-name-only-section")

# The tool-layer gate is installed by the Claude session handler only, so on any
# other backend there is nothing enforcing this policy no matter what the
# installed Claude SDK supports. This text therefore claims no gate at all, and
# it deliberately avoids asserting which primitives the backend does or does not
# expose — that varies per backend and would go stale as they gain features.
_TOOL_POLICY_UNGATED_SECTION = prompt_text("tool-policy-ungated-section")

_SESSION_TITLE_PROMPT = prompt_text("session-title-prompt")


_USER_PREFERENCES_PROMPT = prompt_text("user-preferences-prompt")

# With Memory not admitted this turn, the preferences file is the only durable
# user surface, so it keeps its historical explicit-request behavior. With
# Memory admitted, Memory's managed lifecycle (disclosed, clearable) owns every
# user-fact write — including explicit "remember this" requests — and the file
# drops to read-only unless the user names it as the destination themselves.
_USER_PREFERENCES_PASSIVE_ROUTING = prompt_text("user-preferences-passive-routing")

_USER_PREFERENCES_MEMORY_ADMITTED_ROUTING = prompt_text("user-preferences-memory-admitted-routing")

_USER_PREFERENCES_PASSIVE_USAGE = prompt_text("user-preferences-passive-usage")

_USER_PREFERENCES_MEMORY_ADMITTED_USAGE = prompt_text("user-preferences-memory-admitted-usage")

_USER_PREFERENCES_PASSIVE_UPDATE_GUIDANCE = prompt_text("user-preferences-passive-update-guidance")

_USER_PREFERENCES_MEMORY_ADMITTED_UPDATE_GUIDANCE = prompt_text("user-preferences-memory-admitted-update-guidance")


_MEMORY_CLI_PROMPT = prompt_text("memory-cli-prompt")


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


def _claude_sdk_hooks_available() -> bool:
    """Whether the installed Claude SDK exposes argument-aware tool hooks.

    Imported here instead of at module scope. This module is shared by every
    backend, and the Codex adapter is loaded in tests under a stub ``modules``
    namespace where a Claude-only import fails outright — a top-level import
    would make the shared prompt module unloadable for a backend that has no
    use for the answer. Only the Claude branch of the selector below asks.

    An unimportable compat module reports False, which selects the weaker
    claim; over-claiming enforcement is the direction that actually hurts.
    """
    try:
        from modules.claude_sdk_compat import CLAUDE_SDK_HOOKS_AVAILABLE
    except ImportError:  # pragma: no cover - only reachable off the Claude path
        return False
    return bool(CLAUDE_SDK_HOOKS_AVAILABLE)


def _build_tool_policy_section(backend: str) -> str:
    """Describe backend-native background tools as the runtime actually treats them.

    Three runtimes, three contracts: a non-Claude backend has no tool-layer
    gate because only the Claude session handler installs one, an SDK without
    argument-aware hooks can only refuse whole tool names, and a current SDK
    on Claude enforces the full policy. Announcing more enforcement than exists is the
    dangerous direction — the agent stops self-policing the calls it believes a
    gate already covers — so an unrecognised backend gets the ungated text.
    """
    if backend != "claude":
        return _TOOL_POLICY_UNGATED_SECTION
    if not _claude_sdk_hooks_available():
        return _TOOL_POLICY_NAME_ONLY_SECTION
    return _TOOL_POLICY_ENFORCED_SECTION


def _build_harness_prompt(
    context: MessageContext,
    *,
    enabled_agents: Optional[Iterable[Any]] = None,
    current_agent_backend: Optional[str] = None,
    skill_available: bool = True,
) -> str:
    backend = str(current_agent_backend or "unknown").strip() or "unknown"
    return render_prompt(
        "harness-routing-prompt",
        skill_routing=_HARNESS_SKILL_ROUTING if skill_available else "",
        tool_policy_section=_build_tool_policy_section(backend),
        enabled_agents_table=_format_enabled_agents_table(enabled_agents),
    )


def _build_show_pages_prompt(
    context: MessageContext,
    *,
    avibe_cloud_guidance: str | None = None,
    skill_available: bool = True,
) -> str:
    default_session_id = _extract_default_session_id(context)
    prompt = render_prompt(
        "show-pages-prompt",
        skill_routing=_SHOW_PAGES_SKILL_ROUTING if skill_available else ""
    )
    if avibe_cloud_guidance:
        prompt += f"\n{avibe_cloud_guidance}\n"
    prompt += "\nHistory contract:\n"
    prompt += format_agent_contract(numbered=True, session_id=default_session_id)
    prompt += "\n"
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


def _build_user_preferences_prompt(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
    memory_admitted: bool = False,
) -> str:
    platform = resolve_context_platform(context, fallback_platform=fallback_platform, default="<platform>")
    # The Memory routing only makes sense once the Agent actually has the
    # Memory CLI this turn. With Memory not admitted, pointing user-fact writes
    # at `vibe memory remember` would describe behavior the injected prompt
    # never grants, so the file keeps its historical explicit-request role.
    if memory_admitted:
        user_context_routing = _USER_PREFERENCES_MEMORY_ADMITTED_ROUTING
        preferences_usage = _USER_PREFERENCES_MEMORY_ADMITTED_USAGE
        update_guidance = _USER_PREFERENCES_MEMORY_ADMITTED_UPDATE_GUIDANCE
    else:
        user_context_routing = _USER_PREFERENCES_PASSIVE_ROUTING
        preferences_usage = _USER_PREFERENCES_PASSIVE_USAGE
        update_guidance = _USER_PREFERENCES_PASSIVE_UPDATE_GUIDANCE
    return render_prompt(
        "user-preferences-prompt",
        preferences_path=f"`{paths.get_user_preferences_path()}`",
        platform=platform,
        user_context_routing=user_context_routing,
        preferences_usage=preferences_usage,
        update_guidance=update_guidance,
    )


def build_system_prompt_injection(
    *,
    include_quick_replies: bool = True,
    include_show_pages: bool = True,
    include_codex_generated_images: bool = False,
    include_user_preferences: bool = True,
    include_memory_cli: bool = False,
    avibe_cloud_connected: bool | None = None,
    context: Optional[MessageContext] = None,
    fallback_platform: Optional[str] = None,
    enabled_agents: Optional[Iterable[Any]] = None,
    current_agent_backend: Optional[str] = None,
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

    advertisable_skills = (
        []
        if skills is None
        else [skill for skill in skills if not skill.disable_model_invocation]
    )
    show_pages_skill_available = any(
        skill.name == "use-show-pages" for skill in advertisable_skills
    )
    vault_skill_available = any(
        skill.name == "use-avibe-vault" for skill in advertisable_skills
    )
    harness_skill_available = any(
        skill.name == "use-avibe-harness" for skill in advertisable_skills
    )

    prompt = _BASE_CAPABILITIES_INTRO
    if context is not None:
        prompt += _build_session_start_prompt(context)
    prompt += _BASE_CAPABILITIES_BODY
    if include_codex_generated_images:
        prompt += _build_codex_generated_images_prompt()
    if include_show_pages and context is not None:
        guidance = AVIBE_CLOUD_CONNECT_GUIDANCE if avibe_cloud_connected is False else None
        prompt += _build_show_pages_prompt(
            context,
            avibe_cloud_guidance=guidance,
            skill_available=show_pages_skill_available,
        )
    if include_quick_replies:
        prompt += _QUICK_REPLIES_PROMPT
    if vault_skill_available:
        prompt += _build_vault_prompt(context, fallback_platform=fallback_platform)
    if context is not None:
        prompt += _build_harness_prompt(
            context,
            enabled_agents=enabled_agents,
            current_agent_backend=current_agent_backend,
            skill_available=harness_skill_available,
        )
    if include_user_preferences:
        prompt += _build_user_preferences_prompt(
            context,
            fallback_platform=fallback_platform,
            memory_admitted=include_memory_cli,
        )
    if include_memory_cli:
        prompt += _MEMORY_CLI_PROMPT
    if skills is not None:
        from core.managed_skills import render_skill_catalog_prompt

        prompt += render_skill_catalog_prompt(skills)
    if context is not None:
        prompt += _build_session_end_prompt(context, fallback_platform=fallback_platform)
    return prompt


SYSTEM_PROMPT_INJECTION = build_system_prompt_injection()
