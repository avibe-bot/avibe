"""Helpers for resolving Agent Session metadata carried by message contexts."""

from __future__ import annotations

from typing import Any, Mapping, Optional


def session_model_settings(
    target: Mapping[str, Any] | None,
    *,
    fallback_model: str | None = None,
    fallback_reasoning_effort: str | None = None,
) -> tuple[str | None, str | None, set[str]]:
    """Resolve a Session's settings without materializing them; explicit null is a pin, not inheritance."""
    from storage.session_reclaim import explicit_override_names

    target = target or {}
    explicit = explicit_override_names(target.get("metadata"))
    model = target.get("model") if "model" in explicit else target.get("model") or fallback_model
    effort = (
        target.get("reasoning_effort") if "reasoning_effort" in explicit
        else target.get("reasoning_effort") or fallback_reasoning_effort
    )
    return model, effort, explicit


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


def session_speed_for_context(context: Any) -> Optional[str]:
    """The processing tier the Session this context runs in asks for: ``"fast"``, or None for standard.

    Read from the Session row dispatch copied onto the context (its ``metadata``);
    a context without a Session row, such as an Agent run's, runs at the standard tier.
    """

    from storage.agent_session_rows import session_speed

    payload = getattr(context, "platform_specific", None) or {}
    target = payload.get("agent_session_target")
    return session_speed(target.get("metadata")) if isinstance(target, dict) else None
