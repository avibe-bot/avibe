"""Shared representation for authoritative backend terminal failures."""

from __future__ import annotations

import uuid
from typing import Any

from core.delivery_evidence import DeliveryEvidence
from core.message_output import MessageOutput, terminal_output_for, terminal_turn_output
from vibe.message_types import spec_for

BACKEND_FAILURE_EVENT = "backend_failure"


def is_backend_failure_notification(message_type: str | None, metadata: Any) -> bool:
    """Whether a persisted message is the visible half of a terminal failure."""

    normalized_type = str(message_type or "").strip()
    event = metadata.get("event") if isinstance(metadata, dict) else None
    return (
        event == BACKEND_FAILURE_EVENT
        and event in spec_for(normalized_type)["terminalWhenEvents"]
    )


def _terminal_output(request: Any, output: MessageOutput | None) -> MessageOutput:
    if output is not None:
        return output
    if request is not None:
        return terminal_output_for(request)
    return terminal_turn_output()


def _harness_run_identity(context: Any, request: Any) -> str:
    """The run id a harness execution's failure notice is keyed by, if any."""

    for source in (getattr(request, "context", None), context):
        payload = getattr(source, "platform_specific", None) or {}
        identity = str(payload.get("task_execution_id") or "").strip()
        if identity:
            return identity
    return ""


def _failure_identity(
    context: Any,
    request: Any,
    explicit_id: str | None,
    *,
    authoritative: bool = False,
) -> str:
    identity = str(explicit_id or "").strip()
    # An AUTHORITATIVE id is one the caller derived from the durable run row — the
    # owed-notice drain's. It wins outright, which is what keeps a D1 interruption
    # notice (``interrupt:{run}:{reason}``) from collapsing into the ordinary
    # notice for the same execution.
    if identity and authoritative:
        return identity

    # A harness execution is keyed by its RUN, whichever backend reported the
    # failure. Codex and OpenCode pass their own turn/message/session id at five
    # call sites, and preferring that over the run id meant the live notification
    # and the drain's replay had different identities — so the drain could not see
    # the message the live path had already persisted, and sent a duplicate. The
    # run id is the one identity BOTH paths can derive, the live one from its
    # context and the drain from the durable row.
    #
    # Non-harness contexts have no run to align to, so a backend's explicit id
    # still wins there and that path is unchanged.
    harness_identity = _harness_run_identity(context, request)
    if harness_identity:
        return harness_identity

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
    failure_id_authoritative: bool = False,
) -> MessageOutput:
    """Build the durable, non-settling visible half of a backend failure."""

    backend_name = str(backend or "backend").strip() or "backend"
    terminal = _terminal_output(request, output)
    identity = _failure_identity(
        context, request, failure_id, authoritative=failure_id_authoritative
    )
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
    delivery: DeliveryEvidence | None = None,
    allow_auth_recovery: bool = True,
    failure_id_authoritative: bool = False,
) -> bool:
    """Notify once, then settle one terminal backend failure silently.

    Backend adapters own structured failure recognition. This helper owns the
    shared representation and keeps visible delivery separate from lifecycle
    settlement. The return value is true when auth recovery supplied the visible
    notification.

    ``delivery``, when supplied, is filled in with what the notify attempt actually
    proved. Nothing here can report that otherwise: this function DISCARDS the
    result of ``emit_agent_message`` and returns normally whether or not the notify
    was delivered, so a caller that owes a durable notice had no way to tell a
    delivered notice from a lost one and would ack on a send that never happened.
    Callers with nothing durable at stake pass nothing and are unaffected.
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
        failure_id_authoritative=failure_id_authoritative,
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

    # ``allow_auth_recovery=False`` is for callers replaying a FAILURE THAT ALREADY
    # HAPPENED rather than reporting one as it happens — the owed-notice drain.
    #
    # Two reasons, and the product one decides it. An owed notice is a report about a
    # run that failed in the past, possibly hours ago, possibly already retried
    # several times; the auth-recovery message is an interactive "reset OAuth" button
    # about the state of the backend RIGHT NOW. Replaying a stale 401 as a live
    # prompt invites the user to fix something that may already be fixed, and does it
    # once per failed run.
    #
    # The mechanical reason follows from that: ``maybe_emit_auth_recovery_message``
    # sends and persists its own message and cannot report into ``DeliveryEvidence``
    # (its signature has no such parameter), so a notice delivered through it reads
    # as unacknowledged and walks on to the next ladder rung. Plumbing evidence
    # through it would make the drain's notice BE the auth prompt, which is the
    # behaviour rejected above.
    #
    # Every other caller keeps the default: the interactive 401 path is where the
    # reset button belongs, and it is untouched.
    auth_service = getattr(controller, "agent_auth_service", None) if allow_auth_recovery else None
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

    # ``delivery`` is passed ONLY when a caller asked for it. Sending it
    # unconditionally changed the call signature for every controller-like object
    # that implements ``emit_agent_message`` without the new keyword, which is a
    # breaking change for an optional diagnostic — three suites caught it.
    notify_kwargs: dict[str, Any] = {"output": notification}
    if delivery is not None:
        notify_kwargs["delivery"] = delivery
    try:
        await controller.emit_agent_message(
            context,
            "notify",
            visible,
            **notify_kwargs,
        )
    finally:
        await settle_terminal_failure()
    return False
