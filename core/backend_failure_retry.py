"""User-requested continuation of one authoritative failed Turn.

The notice links to an existing Delivery, not a second execution state machine.
All writers call these helpers while holding the SQLite writer reservation.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import Connection

from core.input_admission import InputEnvelope, admit_input
from storage import message_deliveries as deliveries, messages_service
from storage.delivery_states import policy_for
from storage.message_deliveries import (
    FAILURE_RETRY_HISTORY_KIND,
    failure_retry_binding as retry_binding,
)
from storage.models import agent_sessions, message_deliveries, messages, session_turns

class RetryUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _workspace_notice(conn: Connection, session_id: str, notice_id: str) -> dict[str, Any] | None:
    """A workspace-addressed notice of a failed Turn of ``session_id``, as that Session's notice.

    The notice row stays in the workspace Session (``row_session_id``); its Turn and
    Session are the ones its ``failure_id`` names, never ones a caller supplies.
    """
    from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID

    notice = messages_service.get_message(conn, notice_id, session_id=WORKSPACE_NOTICE_SESSION_ID)
    if notice is None:
        return None
    source_session_id, turn_id = deliveries.workspace_notice_source(conn, notice.get("metadata") or {})
    if source_session_id != session_id or not turn_id:
        return None
    return {
        **notice,
        "session_id": session_id,
        "row_session_id": WORKSPACE_NOTICE_SESSION_ID,
        "metadata": {**(notice.get("metadata") or {}), "turn_id": turn_id},
    }


def notice_retryable(notice: dict[str, Any] | None) -> bool:
    """Whether a notice can carry a retry at all; the read and the press share this."""
    metadata = (notice or {}).get("metadata") or {}
    return not (
        notice is None
        or notice.get("type") != "notify"
        or notice.get("author") != "agent"
        or notice.get("source") != "agent"
        or metadata.get("event") != "backend_failure"
        or not metadata.get("failure_id")
        or not metadata.get("turn_id")
        or metadata.get("detached")
        # The Turn's running commands continue as Watches: a retry would run them twice.
        or metadata.get("watch_ids")
    )


def _notice(conn: Connection, session_id: str, notice_id: str) -> dict[str, Any]:
    notice = messages_service.get_message(conn, notice_id, session_id=session_id)
    if notice is None:
        notice = _workspace_notice(conn, session_id, notice_id)
    if not notice_retryable(notice):
        raise RetryUnavailable("retry_not_available")
    assert notice is not None
    return notice


def _retained_inputs(conn: Connection, turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover the exact unwritten batch, without recreating its snapshots."""
    retained = []
    for row in conn.execute(
        select(message_deliveries)
        .where(message_deliveries.c.session_id == turn["session_id"])
        .where(message_deliveries.c.state == "queued")
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
    ).mappings():
        delivery = dict(row)
        events = json.loads(delivery["delivery_history_json"]).get("events", [])
        attempts = [event for event in events if event.get("kind") == "start"]
        last = attempts[-1] if attempts else {}
        claimed = next(
            (event for event in reversed(attempts) if event.get("outcome") in {"claimed", "opened"}),
            {},
        )
        if (
            claimed.get("turn_id") == turn["id"]
            and last.get("outcome") == "not_written"
            and not delivery.get("message_id")
            and not delivery.get("current_attempt_id")
        ):
            retained.append(delivery)
    if not any(row["id"] == turn["initial_delivery_id"] for row in retained):
        raise RetryUnavailable("retry_original_unavailable")
    return retained


def _validate_boundary(
    conn: Connection,
    notice: dict[str, Any],
    *,
    exclude_ids: set[str],
) -> dict[str, Any]:
    session_id = notice["session_id"]
    turn_id = notice["metadata"]["turn_id"]
    session = conn.execute(select(agent_sessions).where(agent_sessions.c.id == session_id)).mappings().one_or_none()
    turn = deliveries.get_turn(conn, turn_id)
    if (
        session is None
        or session["status"] != "active"
        or session["visibility"] == "system"
        or turn is None
        or turn["session_id"] != session_id
        # A continuation belongs to the backend that ran the Turn. An input that
        # never reached it (``not_written``) belongs to no backend yet, so it may
        # run wherever the session routes now: that is how "continue on Vibey"
        # retries a message whose Agent was disabled.
        or (turn["backend"] != session["agent_backend"] and turn["terminal_outcome"] != "not_written")
    ):
        raise RetryUnavailable("retry_not_available")
    # Stale once anything newer than this failure ran or runs: a live Turn, or a
    # Turn that took an input no failed or stopped Turn carried, or one whose
    # inputs left it (a Turn stopped before its write). Turns of carried inputs
    # only, already ended, do not count: a press must still work after them.
    source_created = conn.execute(
        select(session_turns.c.created_at).where(session_turns.c.id == turn_id)
    ).scalar_one_or_none()
    membership = select(message_deliveries.c.id).where(message_deliveries.c.turn_id == session_turns.c.id)
    newer_work = (
        select(session_turns.c.id)
        .where(session_turns.c.session_id == session_id, session_turns.c.id != turn_id)
        .where(or_(
            session_turns.c.created_at > source_created,
            and_(session_turns.c.created_at == source_created, session_turns.c.id > turn_id),
        ))
        .where(or_(
            session_turns.c.state != "terminal",
            ~membership.exists(),
            membership.where(
                ~message_deliveries.c.delivery_history_json.like('%"kind":"turn_carry"%')
            ).exists(),
        ))
        .limit(1)
    )
    latest = (
        turn_id if source_created is not None and conn.execute(newer_work).first() is None else None
    )
    if latest != turn_id:
        raise RetryUnavailable("retry_stale")
    if turn["state"] != "terminal":
        raise RetryUnavailable("retry_busy")
    if turn["terminal_outcome"] not in {"failed", "not_written"} or turn["settled_by"] in {
        "stopped",
        "agent_run_canceled",
        "session_archive",
    }:
        raise RetryUnavailable("retry_not_available")
    if turn["terminal_outcome"] == "failed" and turn["start_receipt_outcome"] != "accepted":
        raise RetryUnavailable("retry_acceptance_unknown")
    # A reserved/unknown input is just as important as an active Turn: it may
    # already have an owner on its way to admission.
    pending = conn.execute(
        select(message_deliveries.c.id, message_deliveries.c.state, message_deliveries.c.delivery_history_json)
        .where(message_deliveries.c.session_id == session_id)
        .where(message_deliveries.c.id.not_in(exclude_ids))
        .where(message_deliveries.c.state.not_in(("accepted", "retired")))
    ).all()
    # Inputs parked behind a failing model wait for exactly this: the next start.
    if any(
        policy_for(row.state).ordering != "terminal"
        and not deliveries.waiting_for_external_input(dict(row._mapping))
        for row in pending
    ):
        raise RetryUnavailable("retry_pending_input")
    return turn


def existing_retry(
    conn: Connection,
    session_id: str,
    notice_id: str,
    *,
    notice: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The retry this notice already owns, when a repeated click must resolve to it.

    An admitted or reserved retry is returned as it is: a duplicate click wakes the
    same owner (even after a lost response) instead of creating, or re-deciding, a
    second one. A link to a Delivery this notice does not own is refused.
    """
    notice = notice if notice is not None else _notice(conn, session_id, notice_id)
    link = notice["content"].get("failure_retry") or {}
    existing = deliveries.get_delivery(conn, str(link.get("delivery_id") or ""))
    if existing is None or existing["session_id"] != session_id:
        return None
    binding = retry_binding(existing)
    if binding is None or binding.get("notice_id") != notice_id:
        raise RetryUnavailable("retry_not_available")
    if policy_for(existing["state"]).submission == "admitted" or existing["state"] == "reserved":
        # Admission retires a reservation if the boundary changed; leaving a stale
        # one here would strand an ordering fence in front of later normal input.
        return existing
    return None


def reserve_retry(
    conn: Connection,
    *,
    session_id: str,
    notice_id: str,
    envelope: InputEnvelope,
) -> dict[str, Any]:
    """Resolve duplicate clicks or reserve one canonical, draft-independent input.

    Retained inputs keep the admission they already recorded; a new retry input
    is admitted through the input admission entry with its binding as the event.
    """
    notice = _notice(conn, session_id, notice_id)
    existing = existing_retry(conn, session_id, notice_id, notice=notice)
    if existing is not None:
        return existing

    source_turn = deliveries.get_turn(conn, str(notice["metadata"]["turn_id"]))
    original = (
        _retained_inputs(conn, source_turn)
        if source_turn is not None
        and source_turn["session_id"] == session_id
        and source_turn["state"] == "terminal"
        and source_turn["terminal_outcome"] == "not_written"
        else []
    )
    backend = conn.execute(
        select(agent_sessions.c.agent_backend).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    vibey_resume = backend == "vibey" and not original
    turn = _validate_boundary(conn, notice, exclude_ids={row["id"] for row in original})
    delivery_id = str(turn["initial_delivery_id"]) if original else deliveries.new_delivery_id()
    binding = {
        "kind": FAILURE_RETRY_HISTORY_KIND,
        "notice_id": notice_id,
        "source_turn_id": turn["id"],
        "delivery_ids": [row["id"] for row in original] or [delivery_id],
    }
    if original:
        for row in original:
            saved = deliveries.cas_delivery(
                conn,
                row["id"],
                expected_version=row["version"],
                expected_states=("queued",),
                values={},
                history_event=binding,
            )
            if saved is None:
                raise RuntimeError("retry retained input claim lost")
    else:
        retry_input = replace(envelope, dispatch_text="continue")
        if vibey_resume:
            # Vibey resumes its committed transcript. Keep the Delivery as the
            # durable ownership record, but do not create a visible user input.
            metadata = json.loads(str(envelope.snapshot().get("metadata_json") or "{}"))
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(
                {
                    deliveries.VIBEY_RESUME_METADATA_KEY: True,
                    "retry_notice_id": notice_id,
                    deliveries.VIBEY_SOURCE_TURN_METADATA_KEY: turn["id"],
                }
            )
            retry_input = replace(
                envelope,
                sender=replace(envelope.sender, kind="harness"),
                kind="agent_initiated",
                provenance=replace(envelope.provenance, origin="harness"),
                dispatch_text="",
                text=None,
                content=None,
                metadata=metadata,
            )
        admit_input(conn, retry_input, delivery_id=delivery_id, history=binding)
    content = dict(notice["content"])
    # ``failure_source`` is read, never stored (``messages_service._row_to_payload``).
    content.pop("failure_source", None)
    content["failure_retry"] = {"delivery_id": delivery_id}
    conn.execute(
        update(messages)
        .where(messages.c.id == notice_id, messages.c.session_id == notice.get("row_session_id", session_id))
        .values(content_json=json.dumps(content, ensure_ascii=False))
    )
    saved = deliveries.get_delivery(conn, delivery_id)
    assert saved is not None
    return saved


def admission_denial(conn: Connection, delivery: dict[str, Any]) -> str | None:
    """Recheck before queue admission AND every queue-drain native start."""
    binding = retry_binding(delivery, unclaimed_only=True)
    if binding is None:
        return None
    try:
        notice = _notice(conn, delivery["session_id"], binding["notice_id"])
        if notice["metadata"]["turn_id"] != binding["source_turn_id"]:
            raise RetryUnavailable("retry_stale")
        if (notice["content"].get("failure_retry") or {}).get("delivery_id") not in binding["delivery_ids"]:
            raise RetryUnavailable("retry_stale")
        _validate_boundary(conn, notice, exclude_ids=set(binding["delivery_ids"]))
    except RetryUnavailable as error:
        return error.code
    return None


def workspace_notice_retry_available(
    conn: Connection, notice: dict[str, Any], session_id: str, turn_id: str
) -> bool:
    """Whether a retry of this failed Turn would be accepted now (read-only).

    The same boundary ``reserve_retry`` checks, so the workspace thread offers
    Retry only where pressing it can start one.
    """
    if not notice_retryable({**notice, "metadata": {**(notice.get("metadata") or {}), "turn_id": turn_id}}):
        return False
    # An input that never reached the backend stays queued; the retry takes it
    # over (``reserve_retry``), so it is not pending work in the way of one.
    # A read must never fail on a retry it cannot offer: any refusal is "no Retry".
    try:
        turn = deliveries.get_turn(conn, turn_id)
        retained = (
            _retained_inputs(conn, turn)
            if turn is not None
            and turn["session_id"] == session_id
            and turn["state"] == "terminal"
            and turn["terminal_outcome"] == "not_written"
            else []
        )
        _validate_boundary(
            conn,
            {"session_id": session_id, "metadata": {"turn_id": turn_id}},
            exclude_ids={row["id"] for row in retained},
        )
    except RetryUnavailable:
        return False
    # A retained input bound to an Agent Run that has already ended can never
    # start: the start claim retires it (``SessionTurnManager._delivery_agent_runs_can_start``),
    # so a Retry that takes it over would only be retired. Offer none. Read-only
    # here, unlike the claim path's inspection, which may write.
    from storage.background import normalize_run_status
    from storage.models import agent_runs

    for row in retained:
        for run in conn.execute(
            select(agent_runs.c.status, agent_runs.c.cancel_requested).where(agent_runs.c.delivery_id == row["id"])
        ).mappings():
            if run["cancel_requested"] or normalize_run_status(run["status"]) not in {"queued", "running"}:
                return False
    return True
