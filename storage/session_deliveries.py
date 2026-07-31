"""Versioned persistence primitives for Session delivery ownership.

Decision transactions are opened by ``SessionTurnManager``. Callers reserve
SQLite's writer slot before their first read, then use the CAS helpers here;
every mutation consumes and checks ``rowcount``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection

from storage.models import messages, session_deliveries, session_turns

TURN_OWNER_STATES = frozenset({"starting", "active", "quarantined"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_turn_id() -> str:
    return f"trn{uuid.uuid4().hex}"


def new_delivery_id() -> str:
    return f"dlv{uuid.uuid4().hex}"


def new_attempt_id() -> str:
    return f"atm{uuid.uuid4().hex}"


def _one(conn: Connection, query: Any) -> dict[str, Any] | None:
    row = conn.execute(query).mappings().first()
    return dict(row) if row is not None else None


def get_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(conn, select(session_turns).where(session_turns.c.id == turn_id))


def get_delivery(conn: Connection, delivery_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_deliveries).where(session_deliveries.c.id == delivery_id),
    )


def active_turn(conn: Connection, session_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_turns)
        .where(session_turns.c.session_id == session_id)
        .where(session_turns.c.state.in_(TURN_OWNER_STATES))
        .order_by(session_turns.c.created_at.desc(), session_turns.c.id.desc())
        .limit(1),
    )


def fifo_head(conn: Connection, session_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_deliveries)
        .where(session_deliveries.c.session_id == session_id)
        .where(session_deliveries.c.state == "queued")
        .order_by(session_deliveries.c.created_at, session_deliveries.c.id)
        .limit(1),
    )


def queued_message_ids(conn: Connection, session_id: str) -> set[str]:
    """Message ids whose durable owner still controls a queued projection."""

    return {
        str(value)
        for value in conn.execute(
            select(session_deliveries.c.message_id)
            .where(session_deliveries.c.session_id == session_id)
            .where(session_deliveries.c.state.in_(("queued", "steering", "reconciling")))
            .where(session_deliveries.c.message_id.is_not(None))
        ).scalars()
        if value
    }


def insert_turn(
    conn: Connection,
    *,
    turn_id: str,
    session_id: str,
    state: str,
    backend: str,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    values = {
        "id": turn_id,
        "session_id": session_id,
        "state": state,
        "backend": backend,
        "start_attempt_id": None,
        "runtime_key": None,
        "runtime_turn_id": None,
        "native_turn_id": None,
        "terminal_outcome": None,
        "version": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "started_at": None,
        "terminal_at": None,
    }
    result = conn.execute(session_turns.insert().values(**values))
    if result.rowcount != 1:
        raise RuntimeError("session Turn insert did not create exactly one row")
    return values


def insert_delivery(
    conn: Connection,
    *,
    delivery_id: str,
    session_id: str,
    message_id: str | None,
    dispatch_text: str | None = None,
    priority: str,
    state: str,
    target_turn_id: str | None = None,
    successor_turn_id: str | None = None,
    steer_attempt_id: str | None = None,
    expected_native_turn_id: str | None = None,
    receipt_outcome: str | None = None,
    receipt_body: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    values = {
        "id": delivery_id,
        "session_id": session_id,
        "message_id": message_id,
        "dispatch_text": dispatch_text,
        "priority": priority,
        "state": state,
        "target_turn_id": target_turn_id,
        "successor_turn_id": successor_turn_id,
        "steer_attempt_id": steer_attempt_id,
        "expected_native_turn_id": expected_native_turn_id,
        "receipt_outcome": receipt_outcome,
        "receipt_body_json": json.dumps(receipt_body or {}, sort_keys=True),
        "version": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = conn.execute(session_deliveries.insert().values(**values))
    if result.rowcount != 1:
        raise RuntimeError("Session delivery insert did not create exactly one row")
    return values


def cas_turn(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    expected_states: Iterable[str],
    values: dict[str, Any],
) -> dict[str, Any] | None:
    update_values = dict(values)
    update_values["version"] = expected_version + 1
    update_values.setdefault("updated_at", utc_now_iso())
    result = conn.execute(
        update(session_turns)
        .where(session_turns.c.id == turn_id)
        .where(session_turns.c.version == expected_version)
        .where(session_turns.c.state.in_(tuple(expected_states)))
        .values(**update_values)
    )
    if result.rowcount != 1:
        return None
    return get_turn(conn, turn_id)


def cas_delivery(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    expected_states: Iterable[str],
    values: dict[str, Any],
) -> dict[str, Any] | None:
    update_values = dict(values)
    update_values["version"] = expected_version + 1
    update_values.setdefault("updated_at", utc_now_iso())
    result = conn.execute(
        update(session_deliveries)
        .where(session_deliveries.c.id == delivery_id)
        .where(session_deliveries.c.version == expected_version)
        .where(session_deliveries.c.state.in_(tuple(expected_states)))
        .values(**update_values)
    )
    if result.rowcount != 1:
        return None
    return get_delivery(conn, delivery_id)


def claim_start_attempt(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    attempt_id: str,
) -> dict[str, Any] | None:
    result = conn.execute(
        update(session_turns)
        .where(session_turns.c.id == turn_id)
        .where(session_turns.c.version == expected_version)
        .where(session_turns.c.state == "starting")
        .where(session_turns.c.start_attempt_id.is_(None))
        .values(
            start_attempt_id=attempt_id,
            version=expected_version + 1,
            updated_at=utc_now_iso(),
        )
    )
    if result.rowcount != 1:
        return None
    return get_turn(conn, turn_id)


def bind_native_start(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    runtime_key: str | None,
    runtime_turn_id: str | None,
    native_turn_id: str | None,
) -> dict[str, Any] | None:
    now = utc_now_iso()
    return cas_turn(
        conn,
        turn_id,
        expected_version=expected_version,
        expected_states=("starting", "quarantined", "active"),
        values={
            "state": "active",
            "runtime_key": runtime_key,
            "runtime_turn_id": runtime_turn_id,
            "native_turn_id": native_turn_id,
            "started_at": now,
        },
    )


def record_steer_receipt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    outcome: str,
    state: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("steering",),
        values={
            "state": state,
            "receipt_outcome": outcome,
            "receipt_body_json": json.dumps(body or {}, sort_keys=True),
        },
    )


def terminalize_and_claim_successor(
    conn: Connection,
    turn_id: str,
    *,
    outcome: str,
) -> dict[str, Any]:
    """Terminalize exactly ``turn_id`` and claim its P0 successor atomically."""

    turn = get_turn(conn, turn_id)
    owned = list(
        conn.execute(
            select(session_deliveries)
            .where(
                (session_deliveries.c.target_turn_id == turn_id)
                | (session_deliveries.c.successor_turn_id == turn_id)
            )
            .order_by(session_deliveries.c.created_at, session_deliveries.c.id)
        ).mappings()
    )
    preserve_queue = any(
        row["priority"] == "p0"
        and row["message_id"] is None
        and row["state"]
        in {"interrupt_pending", "interrupting", "waiting_terminal", "reconciling", "completed"}
        for row in owned
    )
    existing_successor = next(
        (
            str(row["successor_turn_id"])
            for row in owned
            if row["successor_turn_id"] and row["state"] == "starting"
        ),
        None,
    )
    if turn is None:
        return {
            "changed": False,
            "successor_turn_id": None,
            "delivery_id": None,
            "requeued_delivery_ids": [],
            "preserve_queue": preserve_queue,
        }
    if turn["state"] == "terminal":
        return {
            "changed": False,
            "successor_turn_id": existing_successor,
            "delivery_id": None,
            "requeued_delivery_ids": [],
            "preserve_queue": preserve_queue,
        }
    if turn["state"] not in TURN_OWNER_STATES:
        return {
            "changed": False,
            "successor_turn_id": None,
            "delivery_id": None,
            "requeued_delivery_ids": [],
            "preserve_queue": preserve_queue,
        }

    terminal = cas_turn(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        expected_states=(str(turn["state"]),),
        values={
            "state": "terminal",
            "terminal_outcome": outcome,
            "terminal_at": utc_now_iso(),
        },
    )
    if terminal is None:
        return {
            "changed": False,
            "successor_turn_id": None,
            "delivery_id": None,
            "requeued_delivery_ids": [],
            "preserve_queue": preserve_queue,
        }
    claimed_successor: str | None = None
    claimed_delivery: str | None = None
    requeued_deliveries: list[str] = []
    for raw in owned:
        delivery = dict(raw)
        successor_id = str(delivery.get("successor_turn_id") or "").strip()
        if successor_id and delivery["state"] in {
            "interrupt_pending",
            "interrupting",
            "waiting_terminal",
            "reconciling",
        }:
            successor = get_turn(conn, successor_id)
            if successor is None or successor["state"] != "pending":
                continue
            if claimed_successor is not None:
                retired = cas_turn(
                    conn,
                    successor_id,
                    expected_version=int(successor["version"]),
                    expected_states=("pending",),
                    values={
                        "state": "terminal",
                        "terminal_outcome": "deferred_successor",
                        "terminal_at": utc_now_iso(),
                    },
                )
                if retired is None:
                    raise RuntimeError("extra P0 successor was not retired")
                queued = cas_delivery(
                    conn,
                    str(delivery["id"]),
                    expected_version=int(delivery["version"]),
                    expected_states=(str(delivery["state"]),),
                    values={
                        "state": "queued",
                        "target_turn_id": None,
                        "successor_turn_id": None,
                    },
                )
                if queued is None:
                    raise RuntimeError("extra P0 successor did not retain its delivery")
                requeued_deliveries.append(str(delivery["id"]))
                continue
            started = cas_turn(
                conn,
                successor_id,
                expected_version=int(successor["version"]),
                expected_states=("pending",),
                values={"state": "starting"},
            )
            if started is None:
                continue
            advance_values: dict[str, Any] = {"state": "starting"}
            if delivery.get("receipt_outcome") is None:
                advance_values["receipt_outcome"] = outcome
            advanced = cas_delivery(
                conn,
                str(delivery["id"]),
                expected_version=int(delivery["version"]),
                expected_states=(str(delivery["state"]),),
                values=advance_values,
            )
            if advanced is None:
                raise RuntimeError("successor Turn claimed without its delivery owner")
            claimed_successor = successor_id
            claimed_delivery = str(delivery["id"])
            continue
        if delivery["state"] in {
            "starting",
            "attached",
            "interrupt_pending",
            "interrupting",
            "waiting_terminal",
        } or (delivery["state"] == "reconciling" and delivery["priority"] == "p0"):
            completion_values: dict[str, Any] = {"state": "completed"}
            if delivery.get("receipt_outcome") is None:
                completion_values["receipt_outcome"] = outcome
            completed = cas_delivery(
                conn,
                str(delivery["id"]),
                expected_version=int(delivery["version"]),
                expected_states=(str(delivery["state"]),),
                values=completion_values,
            )
            if completed is None:
                raise RuntimeError("terminal Turn did not settle its delivery owner")

    return {
        "changed": True,
        "successor_turn_id": claimed_successor,
        "delivery_id": claimed_delivery,
        "requeued_delivery_ids": requeued_deliveries,
        "preserve_queue": preserve_queue,
    }


def requeue_prewrite_failure(
    conn: Connection,
    turn_id: str,
    *,
    outcome: str,
) -> dict[str, Any] | None:
    """Release a starting owner when native dispatch provably did not begin."""

    turn = get_turn(conn, turn_id)
    if turn is None or turn["state"] != "starting":
        return None
    delivery = delivery_for_turn(conn, turn_id)
    if delivery is None or delivery["state"] != "starting":
        return None
    queued = cas_delivery(
        conn,
        str(delivery["id"]),
        expected_version=int(delivery["version"]),
        expected_states=("starting",),
        values={
            "state": "queued",
            "target_turn_id": None,
            "successor_turn_id": None,
            "receipt_outcome": outcome,
        },
    )
    if queued is None:
        raise RuntimeError("pre-write failure did not requeue its delivery owner")
    terminal = cas_turn(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        expected_states=("starting",),
        values={
            "state": "terminal",
            "terminal_outcome": outcome,
            "terminal_at": utc_now_iso(),
        },
    )
    if terminal is None:
        raise RuntimeError("pre-write failure did not release its Turn owner")
    return queued


def recovery_turns(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(session_turns).where(session_turns.c.state.in_(TURN_OWNER_STATES))
    if session_id:
        query = query.where(session_turns.c.session_id == session_id)
    query = query.order_by(session_turns.c.created_at, session_turns.c.id)
    return [dict(row) for row in conn.execute(query).mappings()]


def unsettled_attempts(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(session_deliveries).where(
        session_deliveries.c.state.in_(
            ("steering", "reconciling", "interrupt_pending", "interrupting", "waiting_terminal")
        )
    )
    if session_id:
        query = query.where(session_deliveries.c.session_id == session_id)
    query = query.order_by(session_deliveries.c.created_at, session_deliveries.c.id)
    return [dict(row) for row in conn.execute(query).mappings()]


def session_ids_with_live_turns(conn: Connection) -> set[str]:
    return {
        str(value)
        for value in conn.execute(
            select(session_turns.c.session_id).where(session_turns.c.state.in_(TURN_OWNER_STATES))
        ).scalars()
        if value
    }


def session_ids_with_turn_history(conn: Connection) -> set[str]:
    return {
        str(value)
        for value in conn.execute(select(session_turns.c.session_id).distinct()).scalars()
        if value
    }


def pending_interrupt_for_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_deliveries)
        .where(session_deliveries.c.target_turn_id == turn_id)
        .where(session_deliveries.c.state == "interrupt_pending")
        .order_by(session_deliveries.c.created_at, session_deliveries.c.id)
        .limit(1),
    )


def turn_has_delivery_owner(conn: Connection, turn_id: str) -> bool:
    return (
        conn.execute(
            select(session_deliveries.c.id)
            .where(
                (session_deliveries.c.target_turn_id == turn_id)
                | (session_deliveries.c.successor_turn_id == turn_id)
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def retire_session_for_archive(conn: Connection, session_id: str) -> dict[str, int]:
    """Retire durable owners before archive removes unsent Message rows."""

    retired_deliveries = 0
    deliveries = list(
        conn.execute(
            select(session_deliveries)
            .where(session_deliveries.c.session_id == session_id)
            .order_by(session_deliveries.c.created_at, session_deliveries.c.id)
        ).mappings()
    )
    for raw in deliveries:
        delivery = dict(raw)
        values: dict[str, Any] = {
            "state": "completed",
            "message_id": None,
            "target_turn_id": None,
            "successor_turn_id": None,
        }
        if delivery.get("receipt_outcome") is None:
            values["receipt_outcome"] = "archived"
        if (
            cas_delivery(
                conn,
                str(delivery["id"]),
                expected_version=int(delivery["version"]),
                expected_states=(str(delivery["state"]),),
                values=values,
            )
            is None
        ):
            raise RuntimeError("archive delivery retirement lost ownership")
        retired_deliveries += 1

    retired_turns = 0
    turns = list(
        conn.execute(
            select(session_turns)
            .where(session_turns.c.session_id == session_id)
            .where(session_turns.c.state != "terminal")
            .order_by(session_turns.c.created_at, session_turns.c.id)
        ).mappings()
    )
    for raw in turns:
        turn = dict(raw)
        if (
            cas_turn(
                conn,
                str(turn["id"]),
                expected_version=int(turn["version"]),
                expected_states=(str(turn["state"]),),
                values={
                    "state": "terminal",
                    "terminal_outcome": "archived",
                    "terminal_at": utc_now_iso(),
                },
            )
            is None
        ):
            raise RuntimeError("archive Turn retirement lost ownership")
        retired_turns += 1
    return {"deliveries": retired_deliveries, "turns": retired_turns}


def delivery_for_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_deliveries)
        .where(
            and_(
                session_deliveries.c.state.in_(("starting", "attached", "reconciling")),
                (session_deliveries.c.target_turn_id == turn_id)
                | (session_deliveries.c.successor_turn_id == turn_id),
            )
        )
        .order_by(session_deliveries.c.created_at, session_deliveries.c.id)
        .limit(1),
    )


def message_for_delivery(conn: Connection, delivery: dict[str, Any]) -> dict[str, Any] | None:
    message_id = str(delivery.get("message_id") or "").strip()
    if not message_id:
        return None
    return _one(conn, select(messages).where(messages.c.id == message_id))
