"""Shared representation for authoritative backend terminal failures."""

from __future__ import annotations

import uuid
from typing import Any

from core.message_output import MessageOutput, terminal_output_for, terminal_turn_output

BACKEND_FAILURE_EVENT = "backend_failure"


def is_backend_failure_notification(message_type: str | None, metadata: Any) -> bool:
    """Whether a persisted message is the visible half of a terminal failure."""

    return (
        str(message_type or "").strip() == "notify"
        and isinstance(metadata, dict)
        and metadata.get("event") == BACKEND_FAILURE_EVENT
    )


def _terminal_output(request: Any, output: MessageOutput | None) -> MessageOutput:
    if output is not None:
        return output
    if request is not None:
        return terminal_output_for(request)
    return terminal_turn_output()


def _failure_identity(
    context: Any,
    request: Any,
    explicit_id: str | None,
) -> str:
    identity = str(explicit_id or "").strip()
    if identity:
        return identity

    for source in (getattr(request, "context", None), context):
        payload = getattr(source, "platform_specific", None) or {}
        for key in (
            "task_execution_id",
            "turn_token",
            "agent_runtime_turn_token",
        ):
            identity = str(payload.get(key) or "").strip()
            if identity:
                return identity

    if request is not None:
        identity = str(getattr(request, "_backend_failure_id", "") or "").strip()
        if not identity:
            identity = uuid.uuid4().hex
            setattr(request, "_backend_failure_id", identity)
        return identity
    return uuid.uuid4().hex


def backend_failure_notification_output(
    context: Any,
    backend: str,
    *,
    request: Any = None,
    output: MessageOutput | None = None,
    failure_id: str | None = None,
) -> MessageOutput:
    """Build the durable, non-settling visible half of a backend failure."""

    backend_name = str(backend or "backend").strip() or "backend"
    terminal = _terminal_output(request, output)
    identity = _failure_identity(context, request, failure_id)
    metadata = dict(terminal.metadata)
    metadata.update(
        {
            "backend": backend_name,
            "event": BACKEND_FAILURE_EVENT,
            "failure_id": identity,
        }
    )
    return MessageOutput(
        completes_turn=False,
        completes_run=False,
        detached=terminal.detached,
        idempotency_key=f"backend-failure:{identity}",
        activity_id=terminal.activity_id,
        causation_id=terminal.causation_id,
        run_id=terminal.run_id,
        metadata=metadata,
    )


async def emit_backend_failure(
    controller: Any,
    context: Any,
    backend: str,
    diagnostic: str,
    *,
    display_text: str | None = None,
    request: Any = None,
    output: MessageOutput | None = None,
    failure_id: str | None = None,
) -> bool:
    """Notify once, then settle one terminal backend failure silently.

    Backend adapters own structured failure recognition. This helper owns the
    shared representation and keeps visible delivery separate from lifecycle
    settlement. The return value is true when auth recovery supplied the visible
    notification.
    """

    backend_name = str(backend or "backend").strip() or "backend"
    error = str(diagnostic or "").strip() or f"{backend_name} backend failed"
    visible = str(display_text or "").strip() or error
    terminal = _terminal_output(request, output)
    notification = backend_failure_notification_output(
        context,
        backend_name,
        request=request,
        output=terminal,
        failure_id=failure_id,
    )

    async def settle_terminal_failure() -> None:
        await controller.emit_agent_message(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=terminal,
            terminal_error=error,
        )

    auth_service = getattr(controller, "agent_auth_service", None)
    maybe_recover = getattr(auth_service, "maybe_emit_auth_recovery_message", None)
    if callable(maybe_recover):
        try:
            handled_auth = await maybe_recover(
                context,
                backend_name,
                visible,
                output=terminal,
                terminal_error=error,
            )
        except Exception:
            await settle_terminal_failure()
            raise
        if handled_auth:
            return True

    try:
        await controller.emit_agent_message(
            context,
            "notify",
            visible,
            output=notification,
        )
    finally:
        await settle_terminal_failure()
    return False
