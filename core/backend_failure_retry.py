"""Continuation of one authoritative Turn through a Delivery, not a new state machine.

Two producers share it: a user's Retry on a failure notice (the notice links to the
Delivery), and startup recovery continuing a Vibey Turn an Avibe restart cut
(``docs/plans/vibey-restart-auto-resume.md``), which owes no notice. All writers call
these helpers while holding the SQLite writer reservation.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from core.run_settlement import SETTLED_BY_RESTART_RESUMED
from storage import agent_transcript, message_deliveries as deliveries, messages_service
from storage.background import move_agent_run_deliveries_in_connection, normalize_run_status
from storage.delivery_states import policy_for
from storage.message_deliveries import (
    FAILURE_RETRY_HISTORY_KIND,
    RESTART_RESUME_HISTORY_KIND,
    failure_retry_binding as retry_binding,
    restart_resume_binding,
)
from storage.models import agent_runs, agent_sessions, message_deliveries, messages, session_turns

#: Consecutive restart continuations of one task without a committed model response
#: in between; the Turn that would be the next one falls back to the restart notice.
RESTART_RESUME_LIMIT = 3
_PENDING_CONTROL_STATES = frozenset({"pending", "interrupting", "waiting_terminal", "reconciling"})


class RetryUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RestartResumeLost(RuntimeError):
    """The continuation's writes cannot all commit: roll the transaction back."""


def _vibey_resume_snapshot(snapshot: dict[str, Any], *, source_turn_id: str, **markers: Any) -> dict[str, Any]:
    """The hidden control input that resumes Vibey's committed transcript.

    The Delivery stays the durable ownership record, but carries no visible user
    input: Vibey resumes from its transcript instead of consuming a new row.
    """
    resumed = dict(snapshot)
    resumed.update(
        {
            "author": "harness",
            "source": "harness",
            "type": "agent_initiated",
            "content_text": None,
            "content_json": "{}",
        }
    )
    metadata = json.loads(str(resumed.get("metadata_json") or "{}"))
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(
        {
            deliveries.VIBEY_RESUME_METADATA_KEY: True,
            deliveries.VIBEY_SOURCE_TURN_METADATA_KEY: source_turn_id,
            **markers,
        }
    )
    resumed["metadata_json"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return resumed


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
        # The Turn's running commands continue as Watches: a retry would run them twice.
        or metadata.get("watch_ids")
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
        retry_snapshot = dict(snapshot)
        dispatch_text = "continue"
        if vibey_resume:
            retry_snapshot = _vibey_resume_snapshot(
                snapshot, source_turn_id=turn["id"], retry_notice_id=notice_id
            )
            dispatch_text = ""
        deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=retry_snapshot,
            dispatch_text=dispatch_text,
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
    restart = restart_resume_binding(delivery, unclaimed_only=True)
    if restart is not None:
        return _restart_resume_denial(conn, delivery, restart)
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


# --- startup continuation of a Vibey Turn an Avibe restart cut ---------------------


def _restart_chain_length(conn: Connection, turn: dict[str, Any]) -> int:
    """Consecutive restart continuations ending at ``turn`` with no model response since."""
    responded = agent_transcript.last_response(conn, str(turn["session_id"]))
    responded_turn_id = responded.turn_id if responded is not None else None
    count = 0
    current: dict[str, Any] | None = turn
    seen: set[str] = set()
    while current is not None and count < RESTART_RESUME_LIMIT:
        turn_id = str(current["id"])
        if turn_id in seen or turn_id == responded_turn_id:
            break
        seen.add(turn_id)
        initial = deliveries.get_delivery(conn, str(current["initial_delivery_id"]))
        binding = restart_resume_binding(initial) if initial is not None else None
        if binding is None:
            break
        count += 1
        current = deliveries.get_turn(conn, str(binding.get("source_turn_id") or ""))
    return count


def _live_run_ids(conn: Connection, turn_id: str) -> tuple[list[str], bool]:
    """The Turn's live Runs, and whether any of them asked to stop."""
    run_ids = deliveries.accepted_agent_run_ids_for_turn(conn, turn_id)
    if not run_ids:
        return [], False
    rows = conn.execute(
        select(agent_runs.c.id, agent_runs.c.status, agent_runs.c.cancel_requested).where(
            agent_runs.c.id.in_(run_ids)
        )
    ).mappings()
    live: list[str] = []
    stop_requested = False
    for row in rows:
        if normalize_run_status(row["status"]) not in {"queued", "running"}:
            continue
        stop_requested = stop_requested or bool(row["cancel_requested"])
        live.append(str(row["id"]))
    return sorted(live), stop_requested


def restart_resume_plan(
    conn: Connection, turn: dict[str, Any]
) -> tuple[str, agent_transcript.CommittedResponse | None]:
    """How startup recovery settles an accepted Vibey Turn a restart cut.

    ``("continue", None)``: a hidden continuation resumes its transcript.
    ``("final", response)``: its final response was committed; it completed.
    ``("unavailable", None)``: it keeps the restart notice (Stop or replacement
    requested, Session archived or system, Agent off Vibey, start not accepted, or
    the continuation bound reached).
    """
    session = conn.execute(
        select(agent_sessions.c.status, agent_sessions.c.visibility, agent_sessions.c.agent_backend).where(
            agent_sessions.c.id == turn["session_id"]
        )
    ).mappings().one_or_none()
    if (
        session is None
        or session["status"] != "active"
        or session["visibility"] == "system"
        or session["agent_backend"] != "vibey"
        or turn.get("backend") != "vibey"
        or turn.get("state") != "active"
        or turn.get("start_receipt_outcome") != "accepted"
        or turn.get("control_mode")
        or turn.get("control_state") in _PENDING_CONTROL_STATES
    ):
        return "unavailable", None
    _runs, stop_requested = _live_run_ids(conn, str(turn["id"]))
    if stop_requested:
        return "unavailable", None
    final = agent_transcript.committed_final(conn, str(turn["session_id"]), str(turn["id"]))
    if final is not None:
        return "final", final
    if _restart_chain_length(conn, turn) >= RESTART_RESUME_LIMIT:
        return "unavailable", None
    return "continue", None


def reserve_restart_resume(conn: Connection, turn: dict[str, Any]) -> dict[str, Any] | None:
    """Reserve the hidden continuation of a cut Turn and move its Runs to it.

    Returns ``None`` (writing nothing) unless the plan is still ``continue``. The
    continuation keeps the cut Turn's routing: its platform, scope, author, metadata
    (Harness provenance included), admission context, and FIFO place, so input queued
    while the service was down waits behind it.
    """
    plan, _final = restart_resume_plan(conn, turn)
    if plan != "continue":
        return None
    session_id = str(turn["session_id"])
    initial = deliveries.get_delivery(conn, str(turn["initial_delivery_id"]))
    if initial is None:
        return None
    payload = deliveries.execution_delivery_payload(conn, initial)
    metadata = dict(payload.get("metadata") or {})
    metadata.pop("merged_delivery_ids", None)
    metadata.pop("merged_native_message_ids", None)
    platform = str(payload.get("platform") or "avibe")
    snapshot = _vibey_resume_snapshot(
        deliveries.message_snapshot(
            scope_id=payload.get("scope_id"),
            session_id=session_id,
            platform=platform,
            author="harness",
            source="harness",
            message_type="agent_initiated",
            metadata=metadata,
            author_id=payload.get("author_id"),
            author_name=payload.get("author_name"),
            parent_native_message_id=payload.get("parent_native_message_id"),
        ),
        source_turn_id=str(turn["id"]),
    )
    admission = deliveries.delivery_admission_context(initial)
    native_message_id = str(payload.get("native_message_id") or "").strip()
    if platform != "avibe" and native_message_id and not admission.get("processing_indicator_message_id"):
        # The continuation answers the same IM message: its indicator replaces the 👀
        # the dead process left there.
        admission["processing_indicator_message_id"] = native_message_id
    delivery_id = deliveries.new_delivery_id()
    reserved = deliveries.insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=session_id,
        priority="p3",
        state="reserved",
        snapshot=snapshot,
        dispatch_text="",
        history_event={
            "kind": "admission",
            "priority": "p3",
            "state": "reserved",
            **({"context": admission} if admission else {}),
        },
    )
    bound = deliveries.cas_delivery(
        conn,
        delivery_id,
        expected_version=int(reserved["version"]),
        expected_states=("reserved",),
        # The cut Turn's place in the FIFO, ahead of anything queued behind it.
        values={"submitted_at": initial["submitted_at"]},
        history_event={
            "kind": RESTART_RESUME_HISTORY_KIND,
            "source_turn_id": str(turn["id"]),
            "delivery_ids": [delivery_id],
        },
    )
    if bound is None:
        raise RestartResumeLost("restart continuation binding lost")
    run_ids, _stop = _live_run_ids(conn, str(turn["id"]))
    owned = {
        str(row["id"])
        for row in deliveries.deliveries_for_turn(conn, str(turn["id"]))
        if row.get("state") == "accepted" and row.get("turn_id") == turn["id"]
    }
    if run_ids and not move_agent_run_deliveries_in_connection(
        conn, run_ids, from_delivery_ids=owned, to_delivery_id=delivery_id
    ):
        raise RestartResumeLost("restart continuation lost a Run move")
    return bound


def _restart_resume_denial(
    conn: Connection, delivery: dict[str, Any], binding: dict[str, Any]
) -> str | None:
    """The continuation runs only while its cut Turn is still the Session's last word."""
    session_id = str(delivery["session_id"])
    session = conn.execute(
        select(agent_sessions.c.status, agent_sessions.c.visibility, agent_sessions.c.agent_backend).where(
            agent_sessions.c.id == session_id
        )
    ).mappings().one_or_none()
    source = deliveries.get_turn(conn, str(binding.get("source_turn_id") or ""))
    if (
        session is None
        or session["status"] != "active"
        or session["visibility"] == "system"
        or session["agent_backend"] != "vibey"
        or source is None
        or source["session_id"] != session_id
        or source["state"] != "terminal"
        or source["settled_by"] != SETTLED_BY_RESTART_RESUMED
    ):
        return "restart_resume_unavailable"
    latest = conn.execute(
        select(session_turns.c.id)
        .where(session_turns.c.session_id == session_id)
        .order_by(session_turns.c.created_at.desc(), session_turns.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest != source["id"]:
        return "restart_resume_stale"
    return None
