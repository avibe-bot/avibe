"""Shared evidence for whether a turn crossed the native dispatch boundary."""

from __future__ import annotations

from typing import Any, Optional


DISPATCH_PHASE_KEY = "agent_dispatch_phase"
DISPATCH_EVIDENCE_KEY = "agent_dispatch_evidence"
DISPATCH_PHASE_PREWRITE = "prewrite"
DISPATCH_PHASE_ATTEMPTING = "attempting"
DISPATCH_PREWRITE_USER_STOP_KEY = "prewrite_user_stop"


def set_dispatch_phase(
    context: Any,
    phase: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(getattr(context, "platform_specific", None) or {})
    current = payload.get(DISPATCH_EVIDENCE_KEY)
    if evidence is None:
        evidence = current if isinstance(current, dict) else {}
    evidence["phase"] = phase
    payload[DISPATCH_EVIDENCE_KEY] = evidence
    payload[DISPATCH_PHASE_KEY] = phase
    context.platform_specific = payload
    return evidence


def backend_dispatch_attempted(context: Any) -> Optional[bool]:
    payload = getattr(context, "platform_specific", None) or {}
    evidence = payload.get(DISPATCH_EVIDENCE_KEY)
    phase = str(
        (evidence.get("phase") if isinstance(evidence, dict) else None)
        or payload.get(DISPATCH_PHASE_KEY)
        or ""
    )
    if phase == DISPATCH_PHASE_PREWRITE:
        return False
    if phase == DISPATCH_PHASE_ATTEMPTING:
        return True
    return None


def mark_prewrite_user_stop(context: Any) -> None:
    """Publish user-Stop intent to the adapter before canceling its task."""

    payload = dict(getattr(context, "platform_specific", None) or {})
    evidence = payload.get(DISPATCH_EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        evidence = {}
        payload[DISPATCH_EVIDENCE_KEY] = evidence
    context.platform_specific = payload
    evidence[DISPATCH_PREWRITE_USER_STOP_KEY] = True


def prewrite_user_stop_requested(context: Any) -> bool:
    """Return whether the shared Turn owner canceled this prewrite task for Stop."""

    payload = getattr(context, "platform_specific", None) or {}
    evidence = payload.get(DISPATCH_EVIDENCE_KEY)
    return bool(
        isinstance(evidence, dict)
        and evidence.get(DISPATCH_PREWRITE_USER_STOP_KEY) is True
    )


def mark_backend_dispatch_attempted(context: Any) -> None:
    """Record the boundary immediately before an adapter's native write."""

    set_dispatch_phase(context, DISPATCH_PHASE_ATTEMPTING)
