from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import Connection

from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries,
    messages,
    session_turns,
    show_session_events,
)


TURN_OWNER_STATES = ("starting", "active")
FENCE_STATES = (
    "reserved",
    "pending_steer",
    "steering",
    "reconciling_steer",
    "reconciling_migration",
)
CLAIMABLE_QUEUE_STATES = ("queued",)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def turn_now_iso() -> str:
    """Preserve the true order of Turn lifecycle boundaries."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def claimable_fifo_prefix(conn: Connection, session_id: str) -> list[dict[str, Any]]:
    """Queued prefix before the first unresolved ordering fence."""

    rows = conn.execute(
        select(message_deliveries)
        .where(message_deliveries.c.session_id == session_id)
        .where(
            or_(
                and_(
                    message_deliveries.c.priority == "p3",
                    message_deliveries.c.state == "queued",
                ),
                message_deliveries.c.state.in_(FENCE_STATES),
            )
        )
        .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
    ).mappings()
    prefix: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        if current["state"] != "queued":
            break
        prefix.append(delivery_payload(current))
    return prefix


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
        "turn_id": row.get("turn_id"),
        "turn_role": row.get("turn_role"),
        "turn_position": row.get("turn_position"),
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
        "version": row.get("version"),
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
    start_attempt_id: str | None = None,
    dispatch_text: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or turn_now_iso()
    has_started = state in {"starting", "active"}
    resolved_attempt_id = start_attempt_id or new_attempt_id() if has_started else None
    resolved_dispatch_text = dispatch_text if has_started else None
    if has_started and resolved_dispatch_text is None:
        initial = get_delivery(conn, initial_delivery_id)
        resolved_dispatch_text = str((initial or {}).get("dispatch_text") or "")
    values = {
        "id": turn_id,
        "session_id": session_id,
        "initial_delivery_id": initial_delivery_id,
        "state": state,
        "backend": backend,
        "runtime_key": None,
        "runtime_turn_id": None,
        "native_turn_id": None,
        "start_attempt_id": resolved_attempt_id,
        "start_receipt_outcome": "accepted" if state == "active" else None,
        "start_receipt_json": (
            _canonical_json({"kind": "seeded_active"}) if state == "active" else "{}"
        ),
        "dispatch_text": resolved_dispatch_text,
        "dispatch_sha256": (
            _sha256_text(resolved_dispatch_text or "") if has_started else None
        ),
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
        "started_at": timestamp if has_started else None,
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
    turn_id: str | None = None,
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
        "turn_id": turn_id,
        "turn_role": None,
        "turn_position": None,
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
    if next_values.get("state") == "starting" and "started_at" not in next_values:
        next_values["started_at"] = turn_now_iso()
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
    turn = get_turn(conn, turn_id)
    delivery = get_delivery(conn, delivery_id)
    if turn is None or delivery is None or turn["state"] != "starting":
        return None
    if turn.get("initial_delivery_id") != delivery_id:
        return None
    updated_turn = cas_turn(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        expected_states=("starting",),
        values={
            "start_attempt_id": attempt_id,
            "dispatch_text": str(delivery.get("dispatch_text") or ""),
            "dispatch_sha256": _sha256_text(str(delivery.get("dispatch_text") or "")),
        },
    )
    if updated_turn is None:
        return None
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("reserved", "queued", "interrupt_waiting"),
        values={
            "state": "claimed",
            "turn_id": turn_id,
            "turn_role": "initial",
            "turn_position": 0,
            "current_attempt_id": None,
            "current_attempt_kind": None,
            "current_target_turn_id": None,
            "current_expected_native_turn_id": None,
            "current_receipt_outcome": None,
            "current_receipt_json": "{}",
            "current_attempt_opened_at": utc_now_iso(),
        },
        history_event={"kind": "start", "attempt_id": attempt_id, "turn_id": turn_id, "outcome": "opened"},
    )


def claim_start_batch(
    conn: Connection,
    *,
    turn_id: str,
    session_id: str,
    backend: str,
    deliveries: list[dict[str, Any]],
    dispatch_text: str,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Claim one ordered Delivery segment for exactly one native start."""

    if not deliveries:
        raise ValueError("a Turn requires at least one Delivery")
    resolved_attempt_id = attempt_id or new_attempt_id()
    turn = insert_turn(
        conn,
        turn_id=turn_id,
        session_id=session_id,
        initial_delivery_id=str(deliveries[0]["id"]),
        state="starting",
        backend=backend,
        start_attempt_id=resolved_attempt_id,
        dispatch_text=dispatch_text,
    )
    claimed: list[dict[str, Any]] = []
    for position, delivery in enumerate(deliveries):
        row = cas_delivery(
            conn,
            str(delivery["id"]),
            expected_version=int(delivery["version"]),
            expected_states=("reserved", "queued", "interrupt_waiting"),
            values={
                "state": "claimed",
                "turn_id": turn_id,
                "turn_role": "initial",
                "turn_position": position,
                "current_attempt_id": None,
                "current_attempt_kind": None,
                "current_target_turn_id": None,
                "current_expected_native_turn_id": None,
                "current_receipt_outcome": None,
                "current_receipt_json": "{}",
                "current_attempt_opened_at": None,
            },
            history_event={
                "kind": "start",
                "attempt_id": resolved_attempt_id,
                "turn_id": turn_id,
                "position": position,
                "outcome": "claimed",
            },
        )
        if row is None:
            raise RuntimeError("Delivery batch claim lost after writer reservation")
        claimed.append(row)
    return {"turn": turn, "deliveries": claimed}


def activate_waiting_successor(
    conn: Connection,
    *,
    turn: dict[str, Any],
    delivery: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        turn.get("state") != "waiting"
        or delivery.get("state") != "interrupt_waiting"
        or delivery.get("turn_id") != turn.get("id")
    ):
        return None
    attempt_id = new_attempt_id()
    dispatch_text = str(delivery.get("dispatch_text") or "")
    started = cas_turn(
        conn,
        str(turn["id"]),
        expected_version=int(turn["version"]),
        expected_states=("waiting",),
        values={
            "state": "starting",
            "start_attempt_id": attempt_id,
            "start_receipt_outcome": None,
            "start_receipt_json": "{}",
            "dispatch_text": dispatch_text,
            "dispatch_sha256": _sha256_text(dispatch_text),
        },
    )
    if started is None:
        return None
    claimed = cas_delivery(
        conn,
        str(delivery["id"]),
        expected_version=int(delivery["version"]),
        expected_states=("interrupt_waiting",),
        values={"state": "claimed"},
        history_event={
            "kind": "start",
            "attempt_id": attempt_id,
            "turn_id": turn["id"],
            "position": 0,
            "outcome": "claimed_after_interrupt",
        },
    )
    if claimed is None:
        raise RuntimeError("waiting successor Delivery claim lost")
    return started


def open_steer_attempt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    turn_id: str,
    attempt_id: str,
    expected_native_turn_id: str,
) -> dict[str, Any] | None:
    current = get_delivery(conn, delivery_id)
    if (
        current is not None
        and current.get("state") == "pending_steer"
        and current.get("current_attempt_id") != attempt_id
    ):
        return None
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


def open_steer_attempt_batch(
    conn: Connection,
    *,
    deliveries: list[dict[str, Any]],
    turn_id: str,
    attempt_id: str,
    expected_native_turn_id: str,
) -> list[dict[str, Any]]:
    """Claim one ordered queue segment for one native steer call."""

    claimed: list[dict[str, Any]] = []
    for delivery in deliveries:
        saved = open_steer_attempt(
            conn,
            str(delivery["id"]),
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=attempt_id,
            expected_native_turn_id=expected_native_turn_id,
        )
        if saved is None:
            raise RuntimeError("Delivery steer batch claim lost after writer reservation")
        claimed.append(saved)
    return claimed


def open_pending_steer_batch(
    conn: Connection,
    *,
    deliveries: list[dict[str, Any]],
    turn_id: str,
    attempt_id: str,
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for delivery in deliveries:
        saved = cas_delivery(
            conn,
            str(delivery["id"]),
            expected_version=int(delivery["version"]),
            expected_states=("reserved", "queued"),
            values={
                "state": "pending_steer",
                "current_attempt_id": attempt_id,
                "current_attempt_kind": "steer",
                "current_target_turn_id": turn_id,
                "current_expected_native_turn_id": None,
                "current_receipt_outcome": None,
                "current_receipt_json": "{}",
                "current_attempt_opened_at": utc_now_iso(),
            },
            history_event={
                "kind": "steer",
                "attempt_id": attempt_id,
                "turn_id": turn_id,
                "outcome": "pending_native_identity",
            },
        )
        if saved is None:
            raise RuntimeError("Delivery pending-steer batch claim lost")
        pending.append(saved)
    return pending


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
            "start_receipt_outcome": "accepted",
            "start_receipt_json": _canonical_json({"kind": "native_start"}),
            "started_at": turn.get("started_at") or turn_now_iso(),
        },
    )


def mark_start_unknown(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    return cas_turn(
        conn,
        turn_id,
        expected_version=expected_version,
        expected_states=("starting",),
        values={
            "start_receipt_outcome": "unknown",
            "start_receipt_json": _canonical_json(receipt),
        },
    )


def _verified_snapshot(delivery: dict[str, Any]) -> dict[str, Any]:
    snapshot_json = str(delivery.get("snapshot_json") or "")
    if not snapshot_json or _sha256_text(snapshot_json) != delivery.get("snapshot_sha256"):
        raise RuntimeError("Delivery snapshot integrity check failed")
    snapshot = _json_object(snapshot_json)
    required = {"session_id", "platform", "author", "type", "content_json", "metadata_json"}
    if not required <= set(snapshot):
        raise RuntimeError("Delivery snapshot is incomplete")
    return snapshot


def _insert_message(
    conn: Connection,
    *,
    message_id: str,
    snapshot: dict[str, Any],
    accepted_at: str,
) -> None:
    existing = conn.execute(select(messages).where(messages.c.id == message_id)).mappings().first()
    values = {
        "id": message_id,
        "scope_id": snapshot.get("scope_id"),
        "session_id": snapshot["session_id"],
        "platform": snapshot["platform"],
        "author": snapshot["author"],
        "type": snapshot["type"],
        "author_id": snapshot.get("author_id"),
        "author_name": snapshot.get("author_name"),
        "source": snapshot.get("source"),
        "native_message_id": snapshot.get("native_message_id"),
        "parent_native_message_id": snapshot.get("parent_native_message_id"),
        "content_text": snapshot.get("content_text"),
        "content_json": snapshot["content_json"],
        "metadata_json": snapshot["metadata_json"],
        "created_at": snapshot.get("created_at") or accepted_at,
        "updated_at": accepted_at,
        "delivered_at": accepted_at,
        "read_at": snapshot.get("read_at"),
    }
    if existing is None:
        conn.execute(messages.insert().values(**values))
        return
    immutable = tuple(key for key in values if key not in {"updated_at", "delivered_at"})
    if any(existing[key] != values[key] for key in immutable):
        raise RuntimeError("Delivery Message identity collided with different content")


def _merged_initial_snapshot(deliveries: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = [_verified_snapshot(delivery) for delivery in deliveries]
    first = dict(snapshots[0])
    first["created_at"] = deliveries[0]["submitted_at"]
    contents = [_json_object(snapshot.get("content_json")) for snapshot in snapshots]
    texts = [str(snapshot.get("content_text") or "") for snapshot in snapshots]
    visible_text = "\n".join(text for text in texts if text)
    attachments = [
        attachment
        for content in contents
        for attachment in (content.get("attachments") or [])
    ]
    merged_content = dict(contents[0])
    if visible_text:
        merged_content["text"] = visible_text
    elif "text" in merged_content:
        merged_content.pop("text", None)
    if attachments:
        merged_content["attachments"] = attachments
    metadata = _json_object(first.get("metadata_json"))
    if len(deliveries) > 1:
        metadata["merged_delivery_ids"] = [str(delivery["id"]) for delivery in deliveries]
        native_ids = [
            str(snapshot.get("native_message_id") or "")
            for snapshot in snapshots
            if str(snapshot.get("native_message_id") or "")
        ]
        if native_ids:
            metadata["merged_native_message_ids"] = native_ids
    first["content_text"] = visible_text or None
    first["content_json"] = _canonical_json(merged_content)
    first["metadata_json"] = _canonical_json(metadata)
    return first


def materialize_start_acceptance(
    conn: Connection,
    *,
    turn_id: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    turn = get_turn(conn, turn_id)
    if turn is None or turn["state"] not in {"starting", "active", "terminal"}:
        return []
    if turn["state"] == "starting" and turn.get("start_receipt_outcome") is None:
        updated = cas_turn(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            expected_states=("starting",),
            values={
                "start_receipt_outcome": "accepted",
                "start_receipt_json": _canonical_json(evidence),
            },
        )
        if updated is None:
            return []
        turn = updated
    rows = [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(message_deliveries.c.turn_id == turn_id)
            .where(message_deliveries.c.turn_role == "initial")
            .order_by(message_deliveries.c.turn_position, message_deliveries.c.id)
        ).mappings()
    ]
    if not rows:
        raise RuntimeError("active Turn has no initial Delivery batch")
    if all(row["state"] == "accepted" for row in rows):
        return rows
    if any(row["state"] != "claimed" for row in rows):
        raise RuntimeError("initial Delivery batch is only partially materialized")
    message_id = str(turn["initial_delivery_id"])
    accepted_at = turn_now_iso()
    snapshot = _merged_initial_snapshot(rows)
    _insert_message(
        conn,
        message_id=message_id,
        snapshot=snapshot,
        accepted_at=accepted_at,
    )
    accepted: list[dict[str, Any]] = []
    for row in rows:
        saved = cas_delivery(
            conn,
            str(row["id"]),
            expected_version=int(row["version"]),
            expected_states=("claimed",),
            values={
                "state": "accepted",
                "message_id": message_id,
                "snapshot_json": None,
                "dispatch_text": None,
                "materialized_at": accepted_at,
                "current_attempt_id": None,
                "current_attempt_kind": None,
                "current_target_turn_id": None,
                "current_expected_native_turn_id": None,
                "current_receipt_outcome": None,
                "current_receipt_json": "{}",
                "current_attempt_opened_at": None,
            },
            history_event={
                "kind": "start",
                "attempt_id": turn.get("start_attempt_id"),
                "turn_id": turn_id,
                "outcome": "accepted",
                "evidence": evidence,
            },
        )
        if saved is None:
            raise RuntimeError("initial Delivery materialization CAS lost")
        accepted.append(saved)
        conn.execute(
            update(show_session_events)
            .where(show_session_events.c.delivery_id == str(row["id"]))
            .where(show_session_events.c.message_id.is_(None))
            .values(message_id=message_id)
        )
    return accepted


def materialize_acceptance(
    conn: Connection,
    *,
    delivery_id: str,
    expected_attempt_id: str | None,
    turn_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any] | None:
    delivery = get_delivery(conn, delivery_id)
    if delivery is None:
        return None
    if delivery["state"] == "accepted":
        if delivery.get("message_id") == delivery_id and delivery.get("turn_id") == turn_id:
            if conn.execute(select(messages.c.id).where(messages.c.id == delivery_id)).scalar_one_or_none() is None:
                raise RuntimeError("accepted Delivery is missing its Message")
            return delivery
        return None
    if expected_attempt_id is not None and delivery.get("current_attempt_id") != expected_attempt_id:
        return None
    target_turn_id = str(delivery.get("current_target_turn_id") or "")
    if target_turn_id != turn_id:
        return None
    target_turn = get_turn(conn, turn_id)
    if (
        target_turn is None
        or target_turn["session_id"] != delivery["session_id"]
    ):
        return None
    snapshot = _verified_snapshot(delivery)
    now = turn_now_iso()
    position = int(
        conn.execute(
            select(func.coalesce(func.max(message_deliveries.c.turn_position), -1)).where(
                message_deliveries.c.turn_id == turn_id
            )
        ).scalar_one()
    ) + 1
    values = {
        "state": "accepted",
        "message_id": delivery_id,
        "turn_id": turn_id,
        "turn_role": "steer",
        "turn_position": position,
        "snapshot_json": None,
        "dispatch_text": None,
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
            "turn_id": turn_id,
            "outcome": "accepted",
            "evidence": evidence,
        },
    )
    if accepted is None:
        return None

    _insert_message(conn, message_id=delivery_id, snapshot=snapshot, accepted_at=now)
    conn.execute(
        update(show_session_events)
        .where(show_session_events.c.delivery_id == delivery_id)
        .where(show_session_events.c.message_id.is_(None))
        .values(message_id=delivery_id)
    )
    return accepted


def attempt_deliveries(conn: Connection, attempt_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(message_deliveries.c.current_attempt_id == attempt_id)
            .order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        ).mappings()
    ]


def materialize_steer_acceptance(
    conn: Connection,
    *,
    leader_delivery_id: str,
    expected_attempt_id: str,
    turn_id: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    leader = get_delivery(conn, leader_delivery_id)
    if leader is None or leader.get("current_attempt_id") != expected_attempt_id:
        return []
    rows = attempt_deliveries(conn, expected_attempt_id)
    if not rows or any(
        row["state"] not in {"steering", "reconciling_steer"}
        or row.get("current_target_turn_id") != turn_id
        for row in rows
    ):
        return []
    target_turn = get_turn(conn, turn_id)
    if target_turn is None or any(row["session_id"] != target_turn["session_id"] for row in rows):
        return []
    now = turn_now_iso()
    message_id = str(rows[0]["id"])
    snapshot = _merged_initial_snapshot(rows)
    first_position = int(
        conn.execute(
            select(func.coalesce(func.max(message_deliveries.c.turn_position), -1)).where(
                message_deliveries.c.turn_id == turn_id
            )
        ).scalar_one()
    ) + 1
    accepted: list[dict[str, Any]] = []
    for offset, row in enumerate(rows):
        saved = cas_delivery(
            conn,
            str(row["id"]),
            expected_version=int(row["version"]),
            expected_states=(str(row["state"]),),
            values={
                "state": "accepted",
                "message_id": message_id,
                "turn_id": turn_id,
                "turn_role": "steer",
                "turn_position": first_position + offset,
                "snapshot_json": None,
                "dispatch_text": None,
                "materialized_at": now,
                "current_attempt_id": None,
                "current_attempt_kind": None,
                "current_target_turn_id": None,
                "current_expected_native_turn_id": None,
                "current_receipt_outcome": None,
                "current_receipt_json": "{}",
                "current_attempt_opened_at": None,
            },
            history_event={
                "kind": "steer",
                "attempt_id": expected_attempt_id,
                "turn_id": turn_id,
                "outcome": "accepted",
                "evidence": evidence,
            },
        )
        if saved is None:
            raise RuntimeError("steer Delivery batch materialization CAS lost")
        accepted.append(saved)
        conn.execute(
            update(show_session_events)
            .where(show_session_events.c.delivery_id == str(row["id"]))
            .where(show_session_events.c.message_id.is_(None))
            .values(message_id=message_id)
        )
    _insert_message(conn, message_id=message_id, snapshot=snapshot, accepted_at=now)
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
    if kind != "steer":
        return None
    return cas_delivery(
        conn,
        delivery_id,
        expected_version=expected_version,
        expected_states=("steering",),
        values={
            "state": "reconciling_steer",
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
    if next_state in {"queued", "reserved", "retired"}:
        values.update(turn_id=None, turn_role=None, turn_position=None)
    if next_state == "retired":
        values["retired_at"] = utc_now_iso()
    history_kind = str(delivery.get("current_attempt_kind") or "")
    if not history_kind:
        if delivery.get("state") == "claimed":
            history_kind = "start"
        elif delivery.get("state") == "pending_steer":
            history_kind = "steer"
        else:
            history_kind = "interrupt_join"
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
            "terminal_at": turn_now_iso(),
        },
    )
    return {"changed": settled is not None, "turn": settled or turn}


def recovery_turns(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(session_turns).where(session_turns.c.state.in_(("waiting",) + TURN_OWNER_STATES))
    if session_id:
        query = query.where(session_turns.c.session_id == session_id)
    return [dict(row) for row in conn.execute(query.order_by(session_turns.c.created_at, session_turns.c.id)).mappings()]


def unresolved_deliveries(conn: Connection, session_id: str | None = None) -> list[dict[str, Any]]:
    query = select(message_deliveries).where(
        message_deliveries.c.state.in_(FENCE_STATES + ("claimed",))
    )
    if session_id:
        query = query.where(message_deliveries.c.session_id == session_id)
    return [dict(row) for row in conn.execute(query.order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)).mappings()]


def recoverable_reservations(
    conn: Connection,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pre-write producer reservations safe to re-enter through admission."""

    query = select(message_deliveries).where(message_deliveries.c.state == "reserved")
    if session_id:
        query = query.where(message_deliveries.c.session_id == session_id)
    return [
        dict(row)
        for row in conn.execute(
            query.order_by(message_deliveries.c.submitted_at, message_deliveries.c.id)
        ).mappings()
    ]


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


def initial_deliveries_for_turn(conn: Connection, turn_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(message_deliveries.c.turn_id == turn_id)
            .where(message_deliveries.c.turn_role == "initial")
            .order_by(message_deliveries.c.turn_position, message_deliveries.c.id)
        ).mappings()
    ]


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


def retire_queued_for_resubmission(
    conn: Connection,
    session_id: str,
    delivery_id: str,
    *,
    expected_dedupe_key: str,
) -> bool:
    """Retire a recovered primary row and release its native dedupe claim."""

    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] != "queued"
        or delivery.get("dedupe_key") != expected_dedupe_key
        or delivery.get("current_attempt_id") is not None
    ):
        return False
    updated = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=("queued",),
        values={
            "state": "retired",
            "retired_at": utc_now_iso(),
            "dedupe_key": None,
        },
        history_event={
            "kind": "retire",
            "reason": "agent_run_recovery_resubmit",
            "released_dedupe_key": expected_dedupe_key,
        },
    )
    return updated is not None


def agent_run_ids_for_delivery(
    conn: Connection,
    delivery: dict[str, Any],
) -> list[str]:
    """Return the Agent Runs operationally represented by one Delivery."""

    delivery_id = str(delivery.get("id") or "").strip()
    if not delivery_id:
        return []
    return [
        str(run_id)
        for run_id in conn.execute(
            select(agent_runs.c.id)
            .where(agent_runs.c.delivery_id == delivery_id)
            .order_by(agent_runs.c.created_at, agent_runs.c.id)
        ).scalars()
    ]


def accepted_agent_run_ids_for_turn(conn: Connection, turn_id: str) -> list[str]:
    """Return every accepted Agent Run participant owned by an exact Turn."""

    run_ids: list[str] = []
    for delivery in deliveries_for_turn(conn, turn_id):
        if (
            delivery.get("state") != "accepted"
            or delivery.get("turn_id") != turn_id
        ):
            continue
        for run_id in agent_run_ids_for_delivery(conn, delivery):
            if run_id not in run_ids:
                run_ids.append(run_id)
    return run_ids


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
    run_ids = agent_run_ids_for_delivery(conn, delivery)
    if run_ids:
        from storage.background import (
            cancel_queued_agent_run_delivery_in_connection,
        )
        if len(run_ids) != 1:
            return False
        if not cancel_queued_agent_run_delivery_in_connection(
            conn,
            run_ids[0],
            session_id=session_id,
            delivery_id=delivery_id,
        ):
            return False
    return retire_queued(conn, session_id, delivery_id)


def terminal_turn_agent_run_owners(
    conn: Connection,
    session_id: str | None = None,
) -> list[tuple[dict[str, Any], list[str]]]:
    """Return terminal Turns and their accepted Agent Run participants."""

    query = (
        select(
            session_turns.c.id.label("turn_id"),
            session_turns.c.terminal_outcome,
            session_turns.c.settled_by,
            session_turns.c.terminal_evidence_kind,
            session_turns.c.terminal_evidence_json,
            session_turns.c.terminal_at,
            agent_runs.c.id.label("run_id"),
        )
        .join(
            message_deliveries,
            message_deliveries.c.turn_id == session_turns.c.id,
        )
        .join(agent_runs, agent_runs.c.delivery_id == message_deliveries.c.id)
        .where(session_turns.c.state == "terminal")
        .where(message_deliveries.c.state == "accepted")
    )
    if session_id:
        query = query.where(session_turns.c.session_id == session_id)
    owners: dict[str, tuple[dict[str, Any], list[str]]] = {}
    rows = conn.execute(
        query.order_by(session_turns.c.terminal_at, session_turns.c.id)
    ).mappings()
    for row in rows:
        turn_id = str(row["turn_id"])
        run_id = str(row["run_id"])
        owner = owners.setdefault(
            turn_id,
            (
                {
                    "id": turn_id,
                    "terminal_outcome": row["terminal_outcome"],
                    "settled_by": row["settled_by"],
                    "terminal_evidence_kind": row["terminal_evidence_kind"],
                    "terminal_evidence_json": row["terminal_evidence_json"],
                },
                [],
            ),
        )
        if run_id not in owner[1]:
            owner[1].append(run_id)
    return list(owners.values())


def retire_reserved(
    conn: Connection,
    session_id: str,
    delivery_id: str,
    *,
    reason: str,
) -> bool:
    """Retire only a producer reservation that no executor has claimed."""

    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] != "reserved"
        or delivery.get("current_attempt_id")
    ):
        return False
    updated = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=("reserved",),
        values={"state": "retired", "retired_at": utc_now_iso()},
        history_event={"kind": "retire", "reason": reason},
    )
    return updated is not None


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
                    ("reserved", "queued", "pending_steer")
                )
            )
        ).mappings()
    ]
    retired = 0
    for row in rows:
        if cas_delivery(
            conn,
            str(row["id"]),
            expected_version=int(row["version"]),
            expected_states=(str(row["state"]),),
            values={
                "state": "retired",
                "retired_at": utc_now_iso(),
                "turn_id": None,
                "turn_role": None,
                "turn_position": None,
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
                    message_deliveries.c.turn_id == turn_id,
                    message_deliveries.c.current_target_turn_id == turn_id,
                )
            )
            .order_by(
                message_deliveries.c.turn_position,
                message_deliveries.c.submitted_at,
                message_deliveries.c.id,
            )
        ).mappings()
    ]
