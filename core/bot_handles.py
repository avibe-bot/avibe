"""Handle suggestions for new Bots (``docs/plans/bots.md`` D2).

1. An ASCII display name is normalized (``Researcher`` to ``researcher``).
2. Otherwise the main Bot's model, at its lowest reasoning effort, proposes an English codename from the display
   name and role through Model Hub, accepted only if it is a valid handle. The request waits at most 5 seconds.
3. Without a usable model answer, the display name is transliterated with ``anyascii`` (``小爱`` to ``xiaoai``).
4. A collision adds ``-2`` (and so on), skipping reserved handles.

The model call runs in the controller process, which owns the Model Hub gateway (``propose_handle_with_model``,
reached over the internal socket); the Web server falls back to transliteration whenever it cannot get an answer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

from core.bots import (
    HANDLE_SOURCE_MODEL,
    HANDLE_SOURCE_TRANSLITERATION,
    ascii_name_handle,
    fallback_handle_base,
    free_handle,
    model_handle,
)

logger = logging.getLogger(__name__)

MODEL_BUDGET_SECONDS = 5.0
#: Reasoning efforts from lowest to highest; a model's lowest is the first of these it supports.
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_MAX_OUTPUT_TOKENS = 2048
_SYSTEM = (
    "You choose a handle for a new member of the user's team of AI assistants. "
    "Reply with one short English codename and nothing else: 2 to 24 characters, lowercase letters, digits, and "
    "hyphens, starting with a letter. Prefer a familiar abbreviation or a short English word for the role, for "
    "example cso for a chief security officer, or researcher."
)


def lowest_effort(efforts: Any) -> Optional[str]:
    """The lowest reasoning effort a model supports, or None when it takes none."""
    offered = [str(effort) for effort in efforts or () if str(effort).strip()]
    for effort in _EFFORT_ORDER:
        if effort in offered:
            return effort
    return offered[0] if offered else None


def needs_model(display_name: str) -> bool:
    """Whether D2 step 2 applies: the display name does not normalize to a handle by itself."""
    return ascii_name_handle(display_name) is None


async def propose_handle_with_model(
    controller: Any, *, display_name: str, role: str = "", budget_seconds: float = MODEL_BUDGET_SECONDS
) -> Optional[str]:
    """The main Bot's model's codename for a new Bot, or None: no route, no answer in time, or no valid handle."""
    store = getattr(controller, "vibe_agent_store", None)
    runtime = getattr(controller, "model_hub_runtime", None)
    if store is None or runtime is None:
        return None
    main = await asyncio.to_thread(store.get_main_bot_agent)
    model = str(getattr(main, "model", None) or "").strip()
    if not model:
        return None
    scope = f"vibey:bot-handle:{uuid.uuid4().hex[:12]}"
    try:
        return await asyncio.wait_for(
            _propose(runtime, model, scope, display_name=display_name, role=role), timeout=budget_seconds
        )
    except asyncio.TimeoutError:
        logger.info("The Bot handle model did not answer within %.0f seconds", budget_seconds)
        return None
    except Exception:
        logger.info("The Bot handle model had no usable route", exc_info=True)
        return None
    finally:
        retire = getattr(runtime, "retire_process_scope", None)
        if callable(retire):
            try:
                retire("vibey", scope)
            except Exception:
                logger.debug("Could not retire the Bot handle model scope", exc_info=True)


async def _propose(runtime: Any, model: str, scope: str, *, display_name: str, role: str) -> Optional[str]:
    from core.agent_core.ai.provider import ModelRequest, ProviderError, TextDelta
    from core.agent_core.cancel import CancelToken
    from core.agent_core.harness.context import output_tokens
    from core.agent_core.messages import TextBlock, UserMessage
    from modules.agents.vibey.models import registry_providers, selection_from_hop

    launch = await runtime.resolve("vibey", model, process_scope=scope)
    selection = selection_from_hop(launch.to_hop_resolution())
    capabilities = selection.capabilities
    effort = lowest_effort(capabilities.reasoning_efforts) if capabilities.supports_reasoning is not False else None
    prompt = f"Display name: {display_name}\nRole: {role.strip() or '(none)'}"
    request = ModelRequest(
        endpoint=selection.endpoint,
        system=_SYSTEM,
        messages=(UserMessage(content=(TextBlock(text=prompt),)),),
        tools=(),
        max_tokens=output_tokens(capabilities, _MAX_OUTPUT_TOKENS),
        reasoning_effort=effort,
        cache="none",
    )
    adapter = registry_providers(media_loader=None)(selection.endpoint.protocol)
    text: list[str] = []
    try:
        async for event in adapter.stream(request, CancelToken()):
            if isinstance(event, TextDelta):
                text.append(event.delta)
            elif isinstance(event, ProviderError):
                return None
    finally:
        close = getattr(adapter, "aclose", None)
        if close is not None:
            await close()
    return model_handle("".join(text))


def suggest_handle(conn: Any, *, display_name: str, proposal: Optional[str]) -> tuple[str, str]:
    """A free handle and its source (``model`` or ``transliteration``) for a new Bot."""
    ascii_handle = ascii_name_handle(display_name)
    if ascii_handle is not None:
        return free_handle(conn, ascii_handle), HANDLE_SOURCE_TRANSLITERATION
    accepted = model_handle(proposal) if proposal else None
    if accepted is not None:
        return free_handle(conn, accepted), HANDLE_SOURCE_MODEL
    return free_handle(conn, fallback_handle_base(display_name)), HANDLE_SOURCE_TRANSLITERATION
