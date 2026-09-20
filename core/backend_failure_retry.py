"""User-requested continuation of one authoritative failed Turn.

The notice links to an existing Delivery, not a second execution state machine.
All writers call these helpers while holding the SQLite writer reservation.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

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


def _notice(conn: Connection, session_id: str, notice_id: str) -> dict[str, Any]:
    notice = messages_service.get_message(conn, notice_id, session_id=session_id)
    metadata = (notice or {}).get("metadata") or {}
    if (
        notice is None
        or notice["type"] != "notify"
        or notice["author"] != "agent"
        or notice["source"] != "agent"
        or metadata.get("event") != "backend_failure"
        or not metadata.get("failure_id")
        or not metadata.get("turn_id")
        or metadata.get("detached")
    ):
        raise RetryUnavailable("retry_not_available")
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
        or turn["backend"] != session["agent_backend"]
    ):
        raise RetryUnavailable("retry_not_available")
    latest = conn.execute(
        select(session_turns.c.id)
        .where(session_turns.c.session_id == session_id)
        .order_by(session_turns.c.created_at.desc(), session_turns.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()
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
        select(message_deliveries.c.id, message_deliveries.c.state)
        .where(message_deliveries.c.session_id == session_id)
        .where(message_deliveries.c.id.not_in(exclude_ids))
        .where(message_deliveries.c.state.not_in(("accepted", "retired")))
    )
    if any(policy_for(row.state).ordering != "terminal" for row in pending):
        raise RetryUnavailable("retry_pending_input")
    return turn


def reserve_retry(
    conn: Connection,
    *,
    session_id: str,
    notice_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Resolve duplicate clicks or reserve one canonical, draft-independent input."""
    notice = _notice(conn, session_id, notice_id)
    link = notice["content"].get("failure_retry") or {}
    existing = deliveries.get_delivery(conn, str(link.get("delivery_id") or ""))
    if existing is not None and existing["session_id"] == session_id:
        binding = retry_binding(existing)
        if binding is None or binding.get("notice_id") != notice_id:
            raise RetryUnavailable("retry_not_available")
        if policy_for(existing["state"]).submission == "admitted":
            return existing
        if existing["state"] == "reserved":
            # Wake the same owner even after a lost response. Admission retires
            # it if the boundary changed; leaving a stale reservation here would
            # strand an ordering fence in front of subsequent normal input.
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
        deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=snapshot,
            dispatch_text="continue",
            history_event=binding,
        )
    content = dict(notice["content"])
    content["failure_retry"] = {"delivery_id": delivery_id}
    conn.execute(
        update(messages)
        .where(messages.c.id == notice_id, messages.c.session_id == session_id)
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
