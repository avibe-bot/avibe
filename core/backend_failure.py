"""Shared representation for authoritative backend terminal failures."""

from __future__ import annotations

import uuid
from dataclasses import replace
from inspect import Parameter, signature
from typing import Any

from core.delivery_evidence import (
    ACK_EVIDENCE_DELIVERY_ONLY,
    ACK_EVIDENCE_RECEIPT,
    DeliveryEvidence,
)
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
    """The Run id proving that this Turn was entered through Harness, if any."""

    for source in (getattr(request, "context", None), context):
        payload = getattr(source, "platform_specific", None) or {}
        identity = str(payload.get("task_execution_id") or "").strip()
        if identity:
            return identity
        accepted = payload.get("accepted_agent_run_ids")
        if isinstance(accepted, list):
            for value in accepted:
                identity = str(value or "").strip()
                if identity:
                    return identity
    return ""


def _turn_failure_identity(context: Any, request: Any) -> str:
    """Stable user-visible failure identity shared by every Run in one Turn."""

    for source in (getattr(request, "context", None), context):
        payload = getattr(source, "platform_specific", None) or {}
        identity = str(payload.get("turn_token") or "").strip()
        if identity:
            return f"turn:{identity}"
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

    # A Harness failure is user-visible once per TURN, whichever backend reported
    # it and however many Runs that Turn accepted. Codex and OpenCode pass their
    # own turn/message/session ids at five call sites, so the durable turn token is
    # the shared identity both the live notification and every linked Run can carry.
    # Legacy callers without a turn token still fall back to the Harness Run id.
    #
    # Non-harness contexts have no run to align to, so a backend's explicit id
    # still wins there and that path is unchanged.
    harness_identity = _harness_run_identity(context, request)
    if harness_identity:
        return _turn_failure_identity(context, request) or harness_identity

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


def _failure_texts(backend_name: str, diagnostic: Any, display_text: Any) -> tuple[str, str]:
    """The diagnostic and the user-visible body, for the live path and the replay.

    Shared so a replayed notice cannot drift from the notice the live path would
    have shown for the same failure: same fallback when a backend supplies no
    display text, same fallback when it supplies no diagnostic either.
    """

    error = str(diagnostic or "").strip() or f"{backend_name} backend failed"
    visible = str(display_text or "").strip() or error
    return error, visible


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


async def emit_replayed_backend_failure(
    controller: Any,
    context: Any,
    backend: str,
    diagnostic: str,
    *,
    failure_id: str,
    display_text: str | None = None,
    delivery: DeliveryEvidence | None = None,
) -> None:
    """Deliver the visible half of a failure that ALREADY ended. Settles nothing.

    The owed-failure-notice drain replays a failure it read back from a durable
    run row, hours after the run itself ended. That is a different act from
    reporting a failure as it happens, and it used to be expressed as the live
    ``emit_backend_failure`` with its live behaviour switched off piece by piece:
    a non-settling ``output``, an auth-recovery bypass, an explicit identity. Four
    review rounds found settlement, auth and identity defects in the gaps between
    those switches, because the replay still entered the live lifecycle and every
    new live behaviour had to be neutralized again by hand.

    So the replay is its own emitter, and the neutralizations become facts of its
    construction rather than arguments:

    * ONE ``notify``. No ``result``, therefore no turn settlement, no status-bubble
      teardown, no runtime-turn release. A replayed context describes a run that
      ended long ago and carries no runtime-turn token, and
      ``emit_matches_runtime_turn`` fails OPEN for a tokenless context —
      deliberately, so a scheduled or watch run can settle its own turn — so a
      settling replay was adopted by whatever turn happened to be live on the
      target channel and finalized it. The run this notice is about is terminal by
      construction: a notice is owed only once the row is settled.
    * NO auth recovery. This is a report about a possibly hours-old 401, not an
      interactive "reset OAuth" affordance about the backend's state right now, and
      ``maybe_emit_auth_recovery_message`` cannot report into ``DeliveryEvidence``
      anyway, so a notice delivered through it would read as unacknowledged and walk
      on to the next delivery rung.
    * The identity is AUTHORITATIVE, with no way to say otherwise. It comes from the
      durable notice row, so two drain passes over that row produce one identity —
      and an interruption's ``interrupt:{run}:{reason}`` survives instead of
      collapsing into the ordinary notice for the same execution, which is what
      happens when a context-derived run id is allowed to win.

    ``delivery`` is filled in with what the notify attempt actually proved; the
    drain acks its durable notice on that evidence, never on a clean return.
    """

    backend_name = str(backend or "backend").strip() or "backend"
    _error, visible = _failure_texts(backend_name, diagnostic, display_text)
    # Non-settling by construction: ``backend_failure_notification_output`` hardcodes
    # ``completes_turn=False`` / ``completes_run=False``, and it is the only output
    # this function emits.
    notification = backend_failure_notification_output(
        context,
        backend_name,
        failure_id=failure_id,
        failure_id_authoritative=True,
    )
    # ``delivery`` is passed ONLY when a caller asked for it: controller-like objects
    # that implement ``emit_agent_message`` without the keyword must keep working.
    notify_kwargs: dict[str, Any] = {"output": notification}
    if delivery is not None:
        notify_kwargs["delivery"] = delivery
    await controller.emit_agent_message(
        context,
        "notify",
        visible,
        **notify_kwargs,
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
) -> bool:
    """Notify and settle one terminal backend failure.

    Backend adapters own structured failure recognition. This helper owns the
    shared representation and keeps visible delivery separate from lifecycle
    settlement. A live Agent Turn is the primary owner of its user-visible error,
    including a Turn entered through Harness. Linked Runs retain durable owed notices
    only as one Turn-scoped fallback when this notification cannot be acknowledged.
    Non-Harness callers retain immediate auth recovery. The return value is true when
    auth recovery supplied that immediate notification.

    ``delivery``, when supplied, is filled in with what the notify attempt actually
    proved. Harness supplies an evidence object automatically because linked Runs
    owe a durable fallback; other callers opt in when they need the same proof. A
    clean return alone is not enough to distinguish a delivered message from a lost
    one.
    """

    backend_name = str(backend or "backend").strip() or "backend"
    error, visible = _failure_texts(backend_name, diagnostic, display_text)
    terminal = _terminal_output(request, output)
    harness_run_id = _harness_run_identity(context, request)
    notification = backend_failure_notification_output(
        context,
        backend_name,
        request=request,
        output=terminal,
        failure_id=failure_id,
    )
    notification_identity = str(notification.metadata.get("failure_id") or "").strip()
    live_delivery = delivery
    if harness_run_id and live_delivery is None:
        live_delivery = DeliveryEvidence()

    def notification_acknowledged() -> bool:
        if live_delivery is None:
            return False
        evidence = live_delivery.ack_evidence
        if evidence == ACK_EVIDENCE_RECEIPT:
            return True
        return (
            evidence == ACK_EVIDENCE_DELIVERY_ONLY
            and str(getattr(context, "platform", "") or "").strip() != "avibe"
        )

    async def settle_terminal_failure() -> None:
        terminal_output = terminal
        if harness_run_id:
            metadata = dict(terminal.metadata)
            metadata["turn_failure_notification"] = {
                "failure_id": notification_identity,
                "ack_evidence": live_delivery.ack_evidence if live_delivery else None,
                "delivered": notification_acknowledged(),
            }
            terminal_output = replace(terminal, metadata=metadata)
        await controller.emit_agent_message(
            context,
            "result",
            "",
            is_error=True,
            level="silent",
            output=terminal_output,
            terminal_error=error,
        )

    # Auth recovery is unconditional for non-Harness live callers, and there is no
    # switch to turn it off. A real interactive 401 should offer its reset-OAuth
    # affordance immediately; a Harness Turn uses the ordinary backend failure
    # notification so the Run fallback remains backend-neutral.
    #
    # The one caller that must not do that — the owed-notice drain, replaying a
    # possibly hours-old failure it read back from a durable row — does not call this
    # function at all; see ``emit_replayed_backend_failure``. A bypass argument here
    # was the previous shape, and it left the replay inside this lifecycle with its
    # live behaviours switched off one at a time.
    auth_service = getattr(controller, "agent_auth_service", None)
    maybe_recover = getattr(auth_service, "maybe_emit_auth_recovery_message", None)
    if not harness_run_id and callable(maybe_recover):
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

    # ``delivery`` is passed only when the emitter supports it. Harness creates the
    # object automatically, but controller-like test and integration doubles with
    # the legacy signature must keep working.
    notify_kwargs: dict[str, Any] = {"output": notification}
    if live_delivery is not None:
        emit = controller.emit_agent_message
        try:
            parameters = signature(emit).parameters.values()
            accepts_delivery = any(
                parameter.name == "delivery" or parameter.kind is Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_delivery = False
        if accepts_delivery:
            notify_kwargs["delivery"] = live_delivery
    try:
        delivered_id = await controller.emit_agent_message(
            context,
            "notify",
            visible,
            **notify_kwargs,
        )
        if (
            live_delivery is not None
            and live_delivery.delivered_id is None
            and delivered_id is not None
            and not bool((context.platform_specific or {}).get("suppress_delivery"))
        ):
            live_delivery.delivered_id = str(delivered_id)
            live_delivery.send_returned = True
    finally:
        await settle_terminal_failure()
    return False
