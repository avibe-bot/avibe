"""Turn-scoped authorization for the Avibe Memory CLI."""

from __future__ import annotations

from typing import Any

from core.message_context import resolve_context_platform
from modules.im import MessageContext


def configure_memory_cli_access(controller: Any, context: MessageContext) -> bool:
    """Apply turn-scoped Memory CLI authorization without changing the prompt."""

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
