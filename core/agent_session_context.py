"""Helpers for resolving Agent Session metadata carried by message contexts."""

from __future__ import annotations

from typing import Any, Optional


def resolve_context_agent_session_target(context: Any) -> Optional[dict[str, Any]]:
    """Return the persisted Agent Session row selected for this context."""
    payload = getattr(context, "platform_specific", None) or {}
    explicit_target = payload.get("agent_session_target")
    if isinstance(explicit_target, dict):
        return explicit_target
    run_target = payload.get("agent_run_target")
    if not isinstance(run_target, dict) or not run_target.get("agent_session_id"):
        return None
    return {**run_target, "id": run_target["agent_session_id"]}
