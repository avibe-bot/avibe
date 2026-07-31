from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Select, and_, delete, or_, select, update
from sqlalchemy.engine import Connection

from storage.models import (
    agent_sessions,
    message_deliveries,
    messages,
    session_turns,
    show_session_events,
)


TURN_OWNER_STATES = ("starting", "active")
FENCE_STATES = (
    "pending_steer",
    "steering",
    "reconciling_start",
    "reconciling_steer",
)
CLAIMABLE_QUEUE_STATES = ("queued",)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_turn_id() -> str:
    return f"trn_{uuid.uuid4().hex}"


def new_delivery_id() -> str:
    return f"msg_{int(time.time() * 1_000_000):015x}{uuid.uuid4().hex[:8]}"


def new_attempt_id() -> str:
    return f"atm_{uuid.uuid4().hex}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _history(value: Any) -> dict[str, Any]:
    parsed = _json_object(value)
    events = parsed.get("events")
    return {
        "version": 1,
        "events": list(events) if isinstance(events, list) else [],
    }


def _append_history_value(value: Any, event: dict[str, Any]) -> str:
    history = _history(value)
    history["events"].append({"at": utc_now_iso(), **event})
    return _canonical_json(history)


def message_snapshot(
    *,
    scope_id: str | None,
    session_id: str,
    platform: str,
    author: str,
    source: str | None,
    message_type: str | None = None,
    text: str | None = None,
    content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    author_id: str | None = None,
    author_name: str | None = None,
    native_message_id: str | None = None,
    parent_native_message_id: str | None = None,
    read_at: str | None = None,
) -> dict[str, Any]:
    """Build the immutable Message candidate held before native acceptance."""

    body = dict(content or {})
    if text is not None:
        body.setdefault("text", text)
    resolved_type = message_type or ("user" if author == "user" else "assistant")
    if source == "harness" and author == "user" and resolved_type == "user":
        author = "harness"
        resolved_type = "harness"
    return {
        "scope_id": scope_id,
        "session_id": session_id,
        "platform": platform,
        "author": author,
        "type": resolved_type,
        "author_id": author_id,
        "author_name": author_name,
        "source": source,
        "native_message_id": native_message_id,
        "parent_native_message_id": parent_native_message_id,
        "content_text": text if text is not None else body.get("text") or None,
        "content_json": _canonical_json(body),
        "metadata_json": _canonical_json(metadata or {}),
        "read_at": read_at,
    }


def _one(conn: Connection, query: Select[Any]) -> dict[str, Any] | None:
    row = conn.execute(query).mappings().first()
    return dict(row) if row else None


def get_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(conn, select(session_turns).where(session_turns.c.id == turn_id))


def get_delivery(conn: Connection, delivery_id: str) -> dict[str, Any] | None:
    return _one(conn, select(message_deliveries).where(message_deliveries.c.id == delivery_id))


def get_delivery_by_dedupe(conn: Connection, dedupe_key: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(message_deliveries).where(message_deliveries.c.dedupe_key == dedupe_key),
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


def ordering_head(conn: Connection, session_id: str) -> dict[str, Any] | None:
    """Oldest queued row or unresolved row that fences later FIFO work."""

    return _one(
        conn,
        select(message_deliveries)
        .where(message_deliveries.c.session_id == session_id)
        .where(
            or_(
                and_(
                    message_deliveries.c.priority == "p3",
                    message_deliveries.c.state.in_(CLAIMABLE_QUEUE_STATES),
                ),
                message_deliveries.c.state.in_(FENCE_STATES),
            )
        )
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        .limit(1),
    )


def claimable_fifo_head(conn: Connection, session_id: str) -> dict[str, Any] | None:
    head = ordering_head(conn, session_id)
    return head if head is not None and head["state"] == "queued" else None


fifo_head = claimable_fifo_head


def list_queued(conn: Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(message_deliveries)
        .where(message_deliveries.c.session_id == session_id)
        .where(message_deliveries.c.state == "queued")
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
    ).mappings()
    return [delivery_payload(dict(row)) for row in rows]


def delivery_payload(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = _json_object(row.get("snapshot_json"))
    content = _json_object(snapshot.get("content_json"))
    metadata = _json_object(snapshot.get("metadata_json"))
    return {
        "id": row["id"],
        "delivery_id": row["id"],
        "message_id": row.get("message_id"),
        "session_id": row.get("session_id"),
        "scope_id": snapshot.get("scope_id"),
        "platform": snapshot.get("platform"),
        "author": snapshot.get("author"),
        "type": snapshot.get("type"),
        "source": snapshot.get("source"),
        "author_id": snapshot.get("author_id"),
        "author_name": snapshot.get("author_name"),
        "native_message_id": snapshot.get("native_message_id"),
        "parent_native_message_id": snapshot.get("parent_native_message_id"),
        "text": snapshot.get("content_text") or content.get("text") or "",
        "content": content,
        "metadata": metadata,
        "dispatch_text": row.get("dispatch_text") or "",
        "priority": row.get("priority"),
        "state": row.get("state"),
        "created_at": row.get("submitted_at"),
        "submitted_at": row.get("submitted_at"),
        "updated_at": row.get("updated_at"),
        "retired_at": row.get("retired_at"),
    }


def list_queued_page(conn: Connection, session_id: str, *, page_request: Any) -> Any:
    from storage.pagination import page_result_from_limit_plus_one

    query = (
        select(message_deliveries)
        .where(message_deliveries.c.session_id == session_id)
        .where(message_deliveries.c.state == "queued")
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        .limit(page_request.limit + 1)
        .offset(page_request.offset)
    )
    return page_result_from_limit_plus_one(
        [delivery_payload(dict(row)) for row in conn.execute(query).mappings()],
        page_request,
    )


def insert_turn(
    conn: Connection,
    *,
    turn_id: str,
    session_id: str,
    initial_delivery_id: str,
    state: str,
    backend: str,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    values = {
        "id": turn_id,
        "session_id": session_id,
        "initial_delivery_id": initial_delivery_id,
        "state": state,
        "backend": backend,
        "runtime_key": None,
        "runtime_turn_id": None,
        "native_turn_id": None,
        "terminal_outcome": None,
        "settled_by": None,
        "terminal_evidence_kind": None,
        "terminal_evidence_json": "{}",
        "control_state": None,
        "control_mode": None,
        "control_attempt_id": None,
        "control_expected_native_turn_id": None,
        "control_receipt_outcome": None,
        "control_receipt_json": "{}",
        "control_successor_delivery_id": None,
        "control_successor_turn_id": None,
        "version": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "started_at": None,
        "terminal_at": None,
    }
    conn.execute(session_turns.insert().values(**values))
    return values


def insert_delivery(
    conn: Connection,
    *,
    delivery_id: str,
    session_id: str,
    priority: str,
    state: str,
    snapshot: dict[str, Any],
    dispatch_text: str,
    dedupe_key: str | None = None,
    accepted_turn_id: str | None = None,
    current_attempt_id: str | None = None,
    current_attempt_kind: str | None = None,
    current_target_turn_id: str | None = None,
    current_expected_native_turn_id: str | None = None,
    current_receipt_outcome: str | None = None,
    current_receipt: dict[str, Any] | None = None,
    history_event: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    serialized_snapshot = _canonical_json(snapshot)
    history = {"version": 1, "events": []}
    if history_event:
        history["events"].append({"at": timestamp, **history_event})
    values = {
        "id": delivery_id,
        "session_id": session_id,
        "message_id": None,
        "priority": priority,
        "state": state,
        "snapshot_json": serialized_snapshot,
        "snapshot_sha256": _sha256_text(serialized_snapshot),
        "dispatch_text": dispatch_text,
        "dispatch_sha256": _sha256_text(dispatch_text),
        "dedupe_key": dedupe_key,
        "accepted_turn_id": accepted_turn_id,
        "current_attempt_id": current_attempt_id,
        "current_attempt_kind": current_attempt_kind,
        "current_target_turn_id": current_target_turn_id,
        "current_expected_native_turn_id": current_expected_native_turn_id,
        "current_receipt_outcome": current_receipt_outcome,
        "current_receipt_json": _canonical_json(current_receipt or {}),
        "current_attempt_opened_at": timestamp if current_attempt_id else None,
        "delivery_history_json": _canonical_json(history),
        "version": 1,
        "submitted_at": timestamp,
        "updated_at": timestamp,
        "materialized_at": None,
        "retired_at": None,
    }
    conn.execute(message_deliveries.insert().values(**values))
    return values


def enqueue_queued(
    conn: Connection,
    *,
    scope_id: str | None,
    session_id: str,
    text: str,
    dispatch_text: str | None = None,
    platform: str = "avibe",
    author: str = "user",
    source: str | None = "user",
    message_type: str | None = None,
    author_id: str | None = None,
    author_name: str | None = None,
    native_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one P3 submission without creating a transcript Message."""

    delivery_id = new_delivery_id()
    row = insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=session_id,
        priority="p3",
        state="queued",
        snapshot=message_snapshot(
            scope_id=scope_id,
            session_id=session_id,
            platform=platform,
            author=author,
            source=source,
            message_type=message_type,
            text=text,
            metadata=metadata,
            author_id=author_id,
            author_name=author_name,
            native_message_id=native_message_id,
        ),
        dispatch_text=text if dispatch_text is None else dispatch_text,
        dedupe_key=(
            f"{platform}:{native_message_id}" if native_message_id else None
        ),
        history_event={"kind": "submitted", "priority": "p3"},
        now=now,
    )
    return delivery_payload(row)


def cas_turn(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    expected_states: Iterable[str],
    values: dict[str, Any],
) -> dict[str, Any] | None:
    next_values = {**values, "version": expected_version + 1, "updated_at": utc_now_iso()}
    result = conn.execute(
        update(session_turns)
        .where(session_turns.c.id == turn_id)
        .where(session_turns.c.version == expected_version)
        .where(session_turns.c.state.in_(tuple(expected_states)))
        .values(**next_values)
    )
    return get_turn(conn, turn_id) if result.rowcount == 1 else None


def cas_delivery(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    expected_states: Iterable[str],
    values: dict[str, Any],
    history_event: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current = get_delivery(conn, delivery_id)
    if current is None or int(current["version"]) != expected_version:
        return None
    next_values = dict(values)
    if history_event is not None:
        next_values["delivery_history_json"] = _append_history_value(
            current.get("delivery_history_json"),
            history_event,
        )
    next_values.update(version=expected_version + 1, updated_at=utc_now_iso())
    result = conn.execute(
        update(message_deliveries)
        .where(message_deliveries.c.id == delivery_id)
        .where(message_deliveries.c.version == expected_version)
        .where(message_deliveries.c.state.in_(tuple(expected_states)))
        .values(**next_values)
    )
    return get_delivery(conn, delivery_id) if result.rowcount == 1 else None


def open_start_attempt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    turn_id: str,
    attempt_id: str,
) -> dict[str, Any] | None:
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("reserved", "queued", "interrupt_waiting"),
        values={
            "state": "start_attempting",
            "current_attempt_id": attempt_id,
            "current_attempt_kind": "start",
            "current_target_turn_id": turn_id,
            "current_expected_native_turn_id": None,
            "current_receipt_outcome": None,
            "current_receipt_json": "{}",
            "current_attempt_opened_at": utc_now_iso(),
        },
        history_event={"kind": "start", "attempt_id": attempt_id, "turn_id": turn_id, "outcome": "opened"},
    )


def open_steer_attempt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    turn_id: str,
    attempt_id: str,
    expected_native_turn_id: str,
) -> dict[str, Any] | None:
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("reserved", "queued", "pending_steer"),
        values={
            "state": "steering",
            "current_attempt_id": attempt_id,
            "current_attempt_kind": "steer",
            "current_target_turn_id": turn_id,
            "current_expected_native_turn_id": expected_native_turn_id,
            "current_receipt_outcome": None,
            "current_receipt_json": "{}",
            "current_attempt_opened_at": utc_now_iso(),
        },
        history_event={
            "kind": "steer",
            "attempt_id": attempt_id,
            "turn_id": turn_id,
            "expected_native_turn_id": expected_native_turn_id,
            "outcome": "opened",
        },
    )


def bind_native_start(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    runtime_key: str | None,
    runtime_turn_id: str | None,
    native_turn_id: str | None,
) -> dict[str, Any] | None:
    turn = get_turn(conn, turn_id)
    if turn is None:
        return None
    for column, observed in (
        ("runtime_key", runtime_key),
        ("runtime_turn_id", runtime_turn_id),
        ("native_turn_id", native_turn_id),
    ):
        persisted = str(turn.get(column) or "").strip()
        candidate = str(observed or "").strip()
        if persisted and candidate and persisted != candidate:
            return None
    return cas_turn(
        conn,
        turn_id,
        expected_version=expected_version,
        expected_states=("starting", "active"),
        values={
            "state": "active",
            "runtime_key": runtime_key or turn.get("runtime_key"),
            "runtime_turn_id": runtime_turn_id or turn.get("runtime_turn_id"),
            "native_turn_id": native_turn_id or turn.get("native_turn_id"),
            "started_at": turn.get("started_at") or utc_now_iso(),
        },
    )


def materialize_acceptance(
    conn: Connection,
    *,
    delivery_id: str,
    expected_attempt_id: str | None,
    accepted_turn_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    delivery = get_delivery(conn, delivery_id)
    if delivery is None:
        return None
    if delivery["state"] == "accepted":
        if delivery.get("message_id") == delivery_id and delivery.get("accepted_turn_id") == accepted_turn_id:
            if conn.execute(select(messages.c.id).where(messages.c.id == delivery_id)).scalar_one_or_none() is None:
                raise RuntimeError("accepted Delivery is missing its Message")
            return delivery
        return None
    if expected_attempt_id is not None and delivery.get("current_attempt_id") != expected_attempt_id:
        return None
    target_turn_id = str(delivery.get("current_target_turn_id") or "")
    if target_turn_id != accepted_turn_id:
        return None
    target_turn = get_turn(conn, accepted_turn_id)
    if (
        target_turn is None
        or target_turn["session_id"] != delivery["session_id"]
        or (
            delivery.get("current_attempt_kind") == "start"
            and target_turn["initial_delivery_id"] != delivery_id
        )
    ):
        return None
    snapshot_json = str(delivery.get("snapshot_json") or "")
    if not snapshot_json or _sha256_text(snapshot_json) != delivery.get("snapshot_sha256"):
        raise RuntimeError("Delivery snapshot integrity check failed")
    snapshot = _json_object(snapshot_json)
    required = {"session_id", "platform", "author", "type", "content_json", "metadata_json"}
    if not required <= set(snapshot):
        raise RuntimeError("Delivery snapshot is incomplete")
    now = utc_now_iso()
    values = {
        "state": "accepted",
        "message_id": delivery_id,
        "accepted_turn_id": accepted_turn_id,
        "snapshot_json": None,
        "materialized_at": now,
        "current_attempt_id": None,
        "current_attempt_kind": None,
        "current_target_turn_id": None,
        "current_expected_native_turn_id": None,
        "current_receipt_outcome": None,
        "current_receipt_json": "{}",
        "current_attempt_opened_at": None,
    }
    accepted = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=(str(delivery["state"]),),
        values=values,
        history_event={
            "kind": str(delivery.get("current_attempt_kind") or evidence.get("kind") or "start"),
            "attempt_id": delivery.get("current_attempt_id"),
            "turn_id": accepted_turn_id,
            "outcome": "accepted",
            "evidence": evidence,
        },
    )
    if accepted is None:
        return None

    existing = conn.execute(select(messages).where(messages.c.id == delivery_id)).mappings().first()
    if existing is None:
        conn.execute(
            messages.insert().values(
                id=delivery_id,
                scope_id=snapshot.get("scope_id"),
                session_id=snapshot["session_id"],
                platform=snapshot["platform"],
                author=snapshot["author"],
                type=snapshot["type"],
                author_id=snapshot.get("author_id"),
                author_name=snapshot.get("author_name"),
                source=snapshot.get("source"),
                native_message_id=snapshot.get("native_message_id"),
                parent_native_message_id=snapshot.get("parent_native_message_id"),
                content_text=snapshot.get("content_text"),
                content_json=snapshot["content_json"],
                metadata_json=snapshot["metadata_json"],
                created_at=delivery["submitted_at"],
                updated_at=now,
                delivered_at=now,
                read_at=snapshot.get("read_at"),
            )
        )
    else:
        immutable_columns = (
            "scope_id",
            "session_id",
            "platform",
            "author",
            "type",
            "author_id",
            "author_name",
            "source",
            "native_message_id",
            "parent_native_message_id",
            "content_text",
            "content_json",
            "metadata_json",
            "read_at",
        )
        if any(existing[column] != snapshot.get(column) for column in immutable_columns):
            raise RuntimeError("Delivery Message identity collided with different content")
    conn.execute(
        update(show_session_events)
        .where(show_session_events.c.delivery_id == delivery_id)
        .where(show_session_events.c.message_id.is_(None))
        .values(message_id=delivery_id)
    )
    return accepted


def mark_attempt_unknown(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    delivery = get_delivery(conn, delivery_id)
    if delivery is None:
        return None
    kind = str(delivery.get("current_attempt_kind") or "")
    state = "reconciling_start" if kind == "start" else "reconciling_steer"
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("start_attempting", "steering"),
        values={
            "state": state,
            "current_receipt_outcome": "unknown",
            "current_receipt_json": _canonical_json(receipt),
        },
        history_event={
            "kind": kind or "attempt",
            "attempt_id": delivery.get("current_attempt_id"),
            "turn_id": delivery.get("current_target_turn_id"),
            "outcome": "unknown",
            "receipt": receipt,
        },
    )


def record_definitive_attempt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    expected_states: Iterable[str],
    outcome: str,
    next_state: str,
    next_priority: str | None = None,
    next_turn_id: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    delivery = get_delivery(conn, delivery_id)
    if delivery is None:
        return None
    values: dict[str, Any] = {
        "state": next_state,
        "current_attempt_id": None,
        "current_attempt_kind": None,
        "current_target_turn_id": next_turn_id,
        "current_expected_native_turn_id": None,
        "current_receipt_outcome": None,
        "current_receipt_json": "{}",
        "current_attempt_opened_at": None,
    }
    if next_priority:
        values["priority"] = next_priority
    if next_state == "retired":
        values["retired_at"] = utc_now_iso()
    history_kind = str(delivery.get("current_attempt_kind") or "")
    if not history_kind:
        history_kind = "steer" if delivery.get("state") == "pending_steer" else "interrupt_join"
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=expected_states,
        values=values,
        history_event={
            "kind": history_kind,
            "attempt_id": delivery.get("current_attempt_id"),
            "turn_id": delivery.get("current_target_turn_id"),
            "outcome": outcome,
            "receipt": receipt or {},
        },
    )


def terminalize_turn(
    conn: Connection,
    turn_id: str,
    *,
    outcome: str,
    settled_by: str | None,
    evidence_kind: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    turn = get_turn(conn, turn_id)
    if turn is None or turn["state"] == "terminal":
        return {"changed": False, "turn": turn}
    settled = cas_turn(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        expected_states=(str(turn["state"]),),
        values={
            "state": "terminal",
            "terminal_outcome": outcome,
            "settled_by": settled_by,
            "terminal_evidence_kind": evidence_kind,
            "terminal_evidence_json": _canonical_json(evidence or {}),
            "terminal_at": utc_now_iso(),
        },
    )
    return {"changed": settled is not None, "turn": settled or turn}


def recovery_turns(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(session_turns).where(session_turns.c.state.in_(("waiting",) + TURN_OWNER_STATES))
    if session_id:
        query = query.where(session_turns.c.session_id == session_id)
    return [dict(row) for row in conn.execute(query.order_by(session_turns.c.created_at, session_turns.c.id)).mappings()]


def unresolved_deliveries(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(message_deliveries).where(message_deliveries.c.state.in_(FENCE_STATES + ("start_attempting",)))
    if session_id:
        query = query.where(message_deliveries.c.session_id == session_id)
    return [dict(row) for row in conn.execute(query.order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)).mappings()]


def recoverable_reservations(
    conn: Connection,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Reservations whose producer expects restart to complete admission.

    Legacy Harness ``pending`` rows were deliberately retry-owned by their
    originating event. Migration preserves that contract as a reservation, but
    startup must not turn it into an autonomous dispatch.
    """

    query = select(message_deliveries).where(message_deliveries.c.state == "reserved")
    if session_id:
        query = query.where(message_deliveries.c.session_id == session_id)
    rows = [
        dict(row)
        for row in conn.execute(
            query.order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        ).mappings()
    ]
    recoverable: list[dict[str, Any]] = []
    for row in rows:
        events = _history(row.get("delivery_history_json"))["events"]
        migrated_retry = any(
            isinstance(event, dict)
            and event.get("kind") == "migration"
            and event.get("legacy_type") == "pending"
            and event.get("outcome") == "awaiting_retry"
            for event in events
        )
        if not migrated_retry:
            recoverable.append(row)
    return recoverable


def purge_session_graph(conn: Connection, session_id: str) -> None:
    """Delete an explicitly hard-purged Session's deferred Delivery/Turn graph.

    Normal queue removal and archive remain retire-only. This helper exists for
    the pre-existing hard Session teardown path and refuses may-have-written work;
    those owners must first pass through SessionTurnManager reconciliation.
    """

    nonterminal_turn = conn.execute(
        select(session_turns.c.id)
        .where(session_turns.c.session_id == session_id)
        .where(session_turns.c.state != "terminal")
        .limit(1)
    ).scalar_one_or_none()
    ambiguous_delivery = conn.execute(
        select(message_deliveries.c.id)
        .where(message_deliveries.c.session_id == session_id)
        .where(
            message_deliveries.c.state.in_(
                ("start_attempting", "steering", "reconciling_start", "reconciling_steer")
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if nonterminal_turn is not None or ambiguous_delivery is not None:
        raise RuntimeError(
            f"Session {session_id} still has unreconciled durable delivery ownership"
        )
    delivery_ids = select(message_deliveries.c.id).where(
        message_deliveries.c.session_id == session_id
    )
    conn.execute(
        update(show_session_events)
        .where(show_session_events.c.delivery_id.in_(delivery_ids))
        .values(delivery_id=None)
    )
    conn.execute(delete(session_turns).where(session_turns.c.session_id == session_id))
    conn.execute(delete(message_deliveries).where(message_deliveries.c.session_id == session_id))


def session_ids_with_live_turns(conn: Connection) -> set[str]:
    return {
        str(value)
        for value in conn.execute(
            select(session_turns.c.session_id).where(session_turns.c.state.in_(TURN_OWNER_STATES)).distinct()
        ).scalars()
    }


def session_ids_with_turn_history(conn: Connection) -> set[str]:
    return {str(value) for value in conn.execute(select(session_turns.c.session_id).distinct()).scalars()}


def active_runtime_session_ids_for_backend(conn: Connection, backend: str) -> set[str]:
    return {
        str(value)
        for value in conn.execute(
            select(session_turns.c.session_id)
            .where(session_turns.c.backend == backend)
            .where(session_turns.c.state == "active")
            .distinct()
        ).scalars()
    }


def live_turns_for_backend_sessions(
    conn: Connection,
    backend: str,
    session_ids: set[str],
) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    return [
        dict(row)
        for row in conn.execute(
            select(session_turns)
            .where(session_turns.c.backend == backend)
            .where(session_turns.c.session_id.in_(session_ids))
            .where(session_turns.c.state.in_(TURN_OWNER_STATES))
        ).mappings()
    ]


def delivery_for_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    turn = get_turn(conn, turn_id)
    return get_delivery(conn, str((turn or {}).get("initial_delivery_id") or ""))


def message_for_delivery(conn: Connection, delivery: dict[str, Any]) -> dict[str, Any] | None:
    message_id = str(delivery.get("message_id") or "")
    return _one(conn, select(messages).where(messages.c.id == message_id)) if message_id else None


def queued_session_ids_without_live_turns(
    conn: Connection,
    session_id: str | None = None,
    *,
    include_held: bool = False,
) -> list[str]:
    live = select(session_turns.c.id).where(
        session_turns.c.session_id == message_deliveries.c.session_id,
        session_turns.c.state.in_(TURN_OWNER_STATES),
    ).exists()
    query = (
        select(message_deliveries.c.session_id)
        .join(agent_sessions, agent_sessions.c.id == message_deliveries.c.session_id)
        .where(message_deliveries.c.state == "queued")
        .where(agent_sessions.c.status == "active")
        .where(~live)
    )
    if not include_held:
        query = query.where(agent_sessions.c.queue_hold_state == "open")
    if session_id:
        query = query.where(message_deliveries.c.session_id == session_id)
    return [str(value) for value in conn.execute(query.distinct()).scalars()]


def retire_queued(conn: Connection, session_id: str, delivery_id: str) -> bool:
    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] != "queued"
    ):
        return False
    updated = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=("queued",),
        values={"state": "retired", "retired_at": utc_now_iso()},
        history_event={"kind": "retire", "reason": "queue_remove"},
    )
    return updated is not None


def owned_agent_run_id(delivery: dict[str, Any]) -> str | None:
    snapshot = _json_object(delivery.get("snapshot_json"))
    native_message_id = str(snapshot.get("native_message_id") or "")
    if native_message_id.startswith("agent_run:"):
        return native_message_id.removeprefix("agent_run:") or None
    metadata = _json_object(snapshot.get("metadata_json"))
    provenance = metadata.get("scheduled_provenance")
    if not isinstance(provenance, dict):
        return None
    run_id = str(provenance.get("task_execution_id") or "").strip()
    return run_id or None


def retire_queued_with_run(
    conn: Connection,
    session_id: str,
    delivery_id: str,
) -> bool:
    """Retire an exact queued Delivery and its held Agent Run atomically."""

    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] != "queued"
    ):
        return False
    run_id = owned_agent_run_id(delivery)
    if run_id:
        from storage.background import cancel_workbench_queued_agent_run_in_connection

        if not cancel_workbench_queued_agent_run_in_connection(
            conn,
            run_id,
            session_id=session_id,
        ):
            return False
    return retire_queued(conn, session_id, delivery_id)


def retire_queued_agent_run(
    conn: Connection,
    *,
    session_id: str,
    run_id: str,
) -> int:
    """Retire the queued Delivery owned by one canceled Workbench Agent Run."""

    normalized_session_id = str(session_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_session_id or not normalized_run_id:
        return 0
    rows = conn.execute(
        select(message_deliveries)
        .where(message_deliveries.c.session_id == normalized_session_id)
        .where(message_deliveries.c.state == "queued")
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
    ).mappings()
    removed = 0
    for raw_row in rows:
        row = dict(raw_row)
        if owned_agent_run_id(row) == normalized_run_id:
            removed += int(retire_queued(conn, normalized_session_id, str(row["id"])))
    return removed


def retire_not_written(conn: Connection, session_id: str, delivery_id: str, *, reason: str) -> bool:
    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] not in {"reserved", "queued", "pending_steer"}
        or delivery.get("current_attempt_id")
    ):
        return False
    updated = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=(str(delivery["state"]),),
        values={"state": "retired", "retired_at": utc_now_iso()},
        history_event={"kind": "retire", "reason": reason},
    )
    return updated is not None


def retire_for_archive(conn: Connection, session_id: str) -> dict[str, int]:
    """Retire only states proven not written; ambiguity remains reconcilable."""

    rows = [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(message_deliveries.c.session_id == session_id)
            .where(
                message_deliveries.c.state.in_(
                    ("reserved", "queued", "pending_steer", "interrupt_waiting")
                )
            )
        ).mappings()
    ]
    retired = 0
    for row in rows:
        if row["state"] == "interrupt_waiting":
            waiting_turn = get_turn(conn, str(row.get("current_target_turn_id") or ""))
            if waiting_turn is None:
                waiting_turn = _one(
                    conn,
                    select(session_turns).where(
                        session_turns.c.initial_delivery_id == row["id"],
                    ),
                )
            if waiting_turn is not None and waiting_turn["state"] == "waiting":
                terminalize_turn(
                    conn,
                    str(waiting_turn["id"]),
                    outcome="not_written",
                    settled_by="session_archive",
                    evidence_kind="unstarted_successor_retired",
                )
                waiting_turn = get_turn(conn, str(waiting_turn["id"]))
            if (
                waiting_turn is None
                or waiting_turn.get("state") != "terminal"
                or waiting_turn.get("terminal_outcome") != "not_written"
            ):
                continue
        if cas_delivery(
            conn,
            str(row["id"]),
            expected_version=int(row["version"]),
            expected_states=(str(row["state"]),),
            values={
                "state": "retired",
                "retired_at": utc_now_iso(),
                "current_attempt_id": None,
                "current_attempt_kind": None,
                "current_target_turn_id": None,
                "current_expected_native_turn_id": None,
                "current_receipt_outcome": None,
                "current_receipt_json": "{}",
                "current_attempt_opened_at": None,
            },
            history_event={"kind": "retire", "reason": "session_archive"},
        ) is not None:
            retired += 1
    return {"retired": retired}


def set_queue_hold(conn: Connection, session_id: str, *, held: bool) -> bool:
    row = conn.execute(
        select(agent_sessions.c.queue_hold_version).where(agent_sessions.c.id == session_id)
    ).first()
    if row is None:
        return False
    version = int(row[0])
    result = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .where(agent_sessions.c.queue_hold_version == version)
        .values(
            queue_hold_state="held" if held else "open",
            queue_hold_version=version + 1,
            queue_held_at=utc_now_iso() if held else None,
        )
    )
    return result.rowcount == 1


def queue_is_held(conn: Connection, session_id: str) -> bool:
    value = conn.execute(
        select(agent_sessions.c.queue_hold_state).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    return value == "held"


def set_draft(conn: Connection, session_id: str, text: str | None) -> bool:
    now = utc_now_iso()
    result = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .values(
            composer_draft_text=text if text and text.strip() else None,
            composer_draft_updated_at=now if text and text.strip() else None,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def get_draft(conn: Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        select(
            agent_sessions.c.composer_draft_text,
            agent_sessions.c.composer_draft_updated_at,
        ).where(agent_sessions.c.id == session_id)
    ).first()
    if row is None or not row[0]:
        return None
    return {"text": str(row[0]), "updated_at": row[1]}


def pending_control_for_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(session_turns)
        .where(session_turns.c.id == turn_id)
        .where(
            session_turns.c.control_state.in_(
                ("pending", "interrupting", "waiting_terminal", "reconciling")
            )
        ),
    )


def pending_steer_for_turn(conn: Connection, turn_id: str) -> dict[str, Any] | None:
    return _one(
        conn,
        select(message_deliveries)
        .where(message_deliveries.c.current_target_turn_id == turn_id)
        .where(message_deliveries.c.state == "pending_steer")
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        .limit(1),
    )


def turn_has_delivery_owner(conn: Connection, turn_id: str) -> bool:
    turn = get_turn(conn, turn_id)
    return bool(turn and get_delivery(conn, str(turn["initial_delivery_id"])))


def deliveries_for_turn(conn: Connection, turn_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(
                or_(
                    message_deliveries.c.accepted_turn_id == turn_id,
                    message_deliveries.c.current_target_turn_id == turn_id,
                )
            )
            .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        ).mappings()
    ]
