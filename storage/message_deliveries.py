from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from sqlalchemy import Select, and_, func, or_, select, update
from sqlalchemy.engine import Connection

from core.delivery_target import normalize_message_kind
from storage.delivery_states import (
    CLAIMABLE_QUEUE_STATES,
    FENCE_STATES,
    RUN_CANCEL_RETIRE_STATES,
    policy_for,
)
from core.memory.admission_metadata import (
    admitted_user_id as legacy_admitted_user_id,
    legacy_message_kind,
)
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries,
    messages,
    session_turns,
    show_session_events,
)


TURN_OWNER_STATES = ("starting", "active")
WEB_PUSH_USER_KEY_METADATA = "_web_push_user_key"
WEB_PUSH_USER_KEYS_METADATA = "_web_push_user_keys"
WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA = "_web_push_authorization_contexts"


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


def native_dedupe_key(
    platform: str,
    native_message_id: str | None,
    *,
    scope_id: str | None = None,
) -> str | None:
    """Return the canonical live dedupe identity for a native submission."""

    native_id = str(native_message_id or "").strip()
    if not native_id:
        return None
    native_scope = str(scope_id or "").strip()
    if not native_scope:
        return f"{platform}:{native_id}"
    return f"{platform}:scope:{len(native_scope)}:{native_scope}:{native_id}"


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
    message_kind: str | None = None,
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
    filtered_metadata = {
        key: value
        for key, value in (metadata or {}).items()
        if not str(key).startswith("_memory_")
    }
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
        "message_kind": normalize_message_kind(message_kind),
        "content_text": text if text is not None else body.get("text") or None,
        "content_json": _canonical_json(body),
        "metadata_json": _canonical_json(filtered_metadata),
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


def get_delivery_by_native_identity(
    conn: Connection,
    *,
    platform: str,
    native_message_id: str,
    scope_id: str | None,
    session_id: str | None = None,
    normalize_legacy: bool = False,
) -> dict[str, Any] | None:
    """Resolve one native event, including the pre-0045 unscoped key."""

    canonical_key = native_dedupe_key(
        platform,
        native_message_id,
        scope_id=scope_id,
    )
    if canonical_key is None:
        return None
    delivery = get_delivery_by_dedupe(conn, canonical_key)
    if delivery is not None or scope_id is None:
        return delivery

    legacy_key = native_dedupe_key(platform, native_message_id)
    if legacy_key is None or legacy_key == canonical_key:
        return None
    delivery = get_delivery_by_dedupe(conn, legacy_key)
    if delivery is None:
        return None
    if session_id is not None and delivery["session_id"] != session_id:
        return None

    delivery_scope = _json_object(delivery.get("snapshot_json")).get("scope_id")
    if delivery_scope is None and delivery.get("message_id"):
        delivery_scope = conn.execute(
            select(messages.c.scope_id)
            .where(messages.c.id == delivery["message_id"])
            .limit(1)
        ).scalar_one_or_none()
    if delivery_scope != scope_id:
        return None
    if not normalize_legacy:
        return delivery

    result = conn.execute(
        update(message_deliveries)
        .where(message_deliveries.c.id == delivery["id"])
        .where(message_deliveries.c.dedupe_key == legacy_key)
        .values(dedupe_key=canonical_key)
    )
    if result.rowcount != 1:
        return get_delivery_by_dedupe(conn, canonical_key)
    delivery["dedupe_key"] = canonical_key
    return delivery


def delivery_admission_context(delivery: dict[str, Any]) -> dict[str, Any]:
    """Return immutable operational context captured by the admission event."""

    try:
        history = json.loads(str(delivery.get("delivery_history_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    events = history.get("events") if isinstance(history, dict) else None
    for event in events or []:
        if not isinstance(event, dict) or event.get("kind") != "admission":
            continue
        context = event.get("context")
        return dict(context) if isinstance(context, dict) else {}
    return {}


def delivery_has_history_event(delivery: dict[str, Any], *, kind: str) -> bool:
    """Return whether a Delivery recorded an event of the requested kind."""

    events = _history(delivery.get("delivery_history_json"))["events"]
    return any(
        isinstance(event, dict) and str(event.get("kind") or "") == kind
        for event in events
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


def ordering_head(
    conn: Connection,
    session_id: str,
    *,
    include_claimable: bool = True,
) -> dict[str, Any] | None:
    """Oldest eligible queued row or unresolved ordering fence."""

    predicates = [message_deliveries.c.state.in_(FENCE_STATES)]
    if include_claimable:
        predicates.append(
            and_(
                message_deliveries.c.priority == "p3",
                message_deliveries.c.state.in_(CLAIMABLE_QUEUE_STATES),
            )
        )

    return _one(
        conn,
        select(message_deliveries)
        .where(message_deliveries.c.session_id == session_id)
        .where(or_(*predicates))
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


def _delivery_payload_from_snapshot(
    row: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
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
        "message_kind": (
            normalize_message_kind(snapshot.get("message_kind"))
            if "message_kind" in snapshot
            else legacy_message_kind(metadata)
        ),
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


def delivery_payload(row: dict[str, Any]) -> dict[str, Any]:
    return _delivery_payload_from_snapshot(
        row,
        _json_object(row.get("snapshot_json")),
    )


def delivery_has_remote_resource_context(row: dict[str, Any]) -> bool:
    """Return whether an immutable Delivery snapshot records remote origin."""

    metadata = delivery_payload(row).get("metadata")
    return isinstance(metadata, dict) and isinstance(
        metadata.get("resource_user_context"),
        dict,
    )


def public_delivery_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return a Delivery payload without server-owned identity metadata."""

    payload = (
        dict(row)
        if "content" in row and "metadata" in row
        else delivery_payload(row)
    )
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        payload["metadata"] = {
            key: value
            for key, value in metadata.items()
            if key != "resource_user_context"
            and not str(key).startswith(("_web_push_", "_memory_"))
        }
    return payload


_MESSAGE_MERGE_IDENTITY_FIELDS = (
    "scope_id",
    "platform",
    "author",
    "type",
    "source",
    "author_id",
    "author_name",
    "parent_native_message_id",
    "message_kind",
)


def message_merge_identity(value: dict[str, Any]) -> tuple[Any, ...]:
    """Return the Message fields that must stay singular after batching."""

    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        metadata = _json_object(value.get("metadata_json"))
    kind = (
        normalize_message_kind(value.get("message_kind"))
        if "message_kind" in value
        else legacy_message_kind(metadata)
    )
    legacy_author_fence = (
        legacy_admitted_user_id(metadata) if not value.get("author_id") else None
    )
    return (
        *(value.get(field) for field in _MESSAGE_MERGE_IDENTITY_FIELDS[:-1]),
        kind,
        legacy_author_fence,
    )


def has_substantive_input(
    dispatch_text: str | None,
    *,
    has_attachments: bool = False,
) -> bool:
    """Whether an admission contains text or a previously resolved attachment."""

    return bool(str(dispatch_text or "").strip()) or has_attachments


def has_substantive_content(
    delivery: dict[str, Any],
    *,
    has_attachments: bool = False,
) -> bool:
    """Whether a reserved Delivery can produce a meaningful backend input."""

    return has_substantive_input(
        delivery.get("dispatch_text"),
        has_attachments=has_attachments,
    )


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
    message_kind: str | None = None,
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
            message_kind=message_kind,
        ),
        dispatch_text=text if dispatch_text is None else dispatch_text,
        dedupe_key=native_dedupe_key(
            platform,
            native_message_id,
            scope_id=scope_id,
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


def rebind_restored_native_generation(
    conn: Connection,
    turn_id: str,
    *,
    expected_version: int,
    expected_native_turn_id: str,
    restored_native_turn_id: str,
) -> dict[str, Any] | None:
    """Rotate one restored runtime generation and every unresolved exact target."""

    turn = get_turn(conn, turn_id)
    if (
        turn is None
        or turn["state"] != "active"
        or turn.get("start_receipt_outcome") != "accepted"
        or int(turn["version"]) != expected_version
        or str(turn.get("native_turn_id") or "") != expected_native_turn_id
        or not restored_native_turn_id
        or restored_native_turn_id == expected_native_turn_id
    ):
        return None
    turn_values: dict[str, Any] = {"native_turn_id": restored_native_turn_id}
    if (
        turn.get("control_state")
        in {"pending", "interrupting", "waiting_terminal", "reconciling"}
        and str(turn.get("control_expected_native_turn_id") or "")
        == expected_native_turn_id
    ):
        turn_values["control_expected_native_turn_id"] = restored_native_turn_id
    rebound = cas_turn(
        conn,
        turn_id,
        expected_version=expected_version,
        expected_states=("active",),
        values=turn_values,
    )
    if rebound is None:
        return None

    attempts = list(
        conn.execute(
            select(message_deliveries).where(
                message_deliveries.c.current_target_turn_id == turn_id,
                message_deliveries.c.state.in_(("steering", "reconciling_steer")),
                message_deliveries.c.current_expected_native_turn_id
                == expected_native_turn_id,
            )
        ).mappings()
    )
    for attempt in attempts:
        saved = cas_delivery(
            conn,
            str(attempt["id"]),
            expected_version=int(attempt["version"]),
            expected_states=(str(attempt["state"]),),
            values={
                "current_expected_native_turn_id": restored_native_turn_id,
            },
            history_event={
                "kind": "native_generation_rebind",
                "attempt_id": attempt.get("current_attempt_id"),
                "turn_id": turn_id,
                "previous_native_turn_id": expected_native_turn_id,
                "restored_native_turn_id": restored_native_turn_id,
            },
        )
        if saved is None:
            raise RuntimeError("restored native generation lost a Delivery attempt CAS")
    return rebound


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
    from storage.messages_service import canonical_message_timestamp

    existing = conn.execute(select(messages).where(messages.c.id == message_id)).mappings().first()
    normalized_accepted_at = canonical_message_timestamp(accepted_at)
    normalized_created_at = canonical_message_timestamp(
        snapshot.get("created_at") or accepted_at
    )
    assert normalized_accepted_at is not None
    assert normalized_created_at is not None
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
        "created_at": normalized_created_at,
        "updated_at": normalized_accepted_at,
        "delivered_at": normalized_accepted_at,
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
    expected_identity = message_merge_identity(snapshots[0])
    if any(message_merge_identity(snapshot) != expected_identity for snapshot in snapshots[1:]):
        raise RuntimeError("Delivery batch contains incompatible Message identities")
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
    web_push_user_keys: list[str] = []
    authorization_contexts: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        snapshot_metadata = _json_object(snapshot.get("metadata_json"))
        values = [snapshot_metadata.get(WEB_PUSH_USER_KEY_METADATA)]
        plural = snapshot_metadata.get(WEB_PUSH_USER_KEYS_METADATA)
        if isinstance(plural, list):
            values.extend(plural)
        for value in values:
            key = str(value or "").strip()
            if key and key not in web_push_user_keys:
                web_push_user_keys.append(key)
        raw_contexts = snapshot_metadata.get(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA)
        if isinstance(raw_contexts, list):
            for raw_context in raw_contexts:
                if not isinstance(raw_context, dict):
                    continue
                user_key = str(raw_context.get("user_key") or "").strip()
                if user_key:
                    authorization_contexts[user_key] = raw_context
    metadata.pop(WEB_PUSH_USER_KEY_METADATA, None)
    metadata.pop(WEB_PUSH_USER_KEYS_METADATA, None)
    if len(web_push_user_keys) == 1:
        metadata[WEB_PUSH_USER_KEY_METADATA] = web_push_user_keys[0]
    elif web_push_user_keys:
        metadata[WEB_PUSH_USER_KEYS_METADATA] = web_push_user_keys
    filtered_contexts = [
        context
        for user_key, context in authorization_contexts.items()
        if user_key in web_push_user_keys
    ]
    if filtered_contexts:
        metadata[WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA] = filtered_contexts
    else:
        metadata.pop(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA, None)
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


def claimed_workbench_message_payload(
    conn: Connection,
    turn_id: str,
) -> dict[str, Any] | None:
    """Project one claimed Web batch exactly as native acceptance will merge it."""

    deliveries = initial_deliveries_for_turn(conn, turn_id)
    if not deliveries or any(delivery["state"] != "claimed" for delivery in deliveries):
        return None
    snapshot = _merged_initial_snapshot(deliveries)
    if not (
        snapshot.get("platform") == "avibe"
        and snapshot.get("author") == "user"
        and snapshot.get("type") == "user"
        and snapshot.get("source") == "user"
    ):
        return None
    return _delivery_payload_from_snapshot(deliveries[0], snapshot)


def materialize_start_acceptance(
    conn: Connection,
    *,
    turn_id: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    turn = get_turn(conn, turn_id)
    if turn is None or turn["state"] not in {"starting", "active", "terminal"}:
        return []
    if turn.get("start_receipt_outcome") != "accepted":
        return []
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
    _insert_message(conn, message_id=message_id, snapshot=snapshot, accepted_at=now)
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
    return accepted


def mark_attempt_receipt(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    outcome: Literal["accepted", "unknown"],
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if outcome not in {"accepted", "unknown"}:
        raise ValueError(f"unsupported steer receipt outcome: {outcome}")
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
            "current_receipt_outcome": outcome,
            "current_receipt_json": _canonical_json(receipt),
        },
        history_event={
            "kind": kind or "attempt",
            "attempt_id": delivery.get("current_attempt_id"),
            "turn_id": delivery.get("current_target_turn_id"),
            "outcome": outcome,
            "receipt": receipt,
        },
    )


def mark_attempt_unknown(
    conn: Connection,
    delivery_id: str,
    *,
    expected_version: int,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    return mark_attempt_receipt(
        conn,
        delivery_id,
        expected_version=expected_version,
        outcome="unknown",
        receipt=receipt,
    )


def mark_attempt_receipt_batch(
    conn: Connection,
    *,
    leader_delivery_id: str,
    outcome: Literal["accepted", "unknown"],
    receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    leader = get_delivery(conn, leader_delivery_id)
    attempt_id = str((leader or {}).get("current_attempt_id") or "")
    if not attempt_id:
        return []
    rows = attempt_deliveries(conn, attempt_id)
    if not rows or any(
        row["state"] not in {"steering", "reconciling_steer"}
        or (
            row["state"] == "reconciling_steer"
            and row.get("current_receipt_outcome") != outcome
        )
        for row in rows
    ):
        return []
    saved_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["state"] == "reconciling_steer":
            saved_rows.append(row)
            continue
        saved = mark_attempt_receipt(
            conn,
            str(row["id"]),
            expected_version=int(row["version"]),
            outcome=outcome,
            receipt=receipt,
        )
        if saved is None:
            raise RuntimeError("steer Delivery batch receipt persistence CAS lost")
        saved_rows.append(saved)
    return saved_rows


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
    values = {
        "state": "terminal",
        "terminal_outcome": outcome,
        "settled_by": settled_by,
        "terminal_evidence_kind": evidence_kind,
        "terminal_evidence_json": _canonical_json(evidence or {}),
        "terminal_at": turn_now_iso(),
    }
    if outcome == "not_written" and turn.get("start_attempt_id"):
        if turn.get("start_receipt_outcome") == "accepted":
            return {"changed": False, "turn": turn}
        values.update(
            {
                "start_receipt_outcome": "not_written",
                "start_receipt_json": _canonical_json(
                    {"kind": evidence_kind, **(evidence or {})}
                ),
            }
        )
    settled = cas_turn(
        conn,
        turn_id,
        expected_version=int(turn["version"]),
        expected_states=(str(turn["state"]),),
        values=values,
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


def agent_run_exclusively_owns_turn(
    conn: Connection,
    *,
    run_id: str,
    turn_id: str,
) -> tuple[bool, str]:
    """Whether stopping ``run_id`` may safely interrupt the exact Turn.

    A Run may own either the sole claimed input of a starting Turn or the sole
    accepted input of an active Turn. Steers, claimed batch siblings, and live
    replacement control are independent participants. The caller holds SQLite's
    writer reservation while asking, so no participant can slip between this
    proof and the P0 control-slot write.
    """

    normalized_run_id = str(run_id or "").strip()
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_run_id or not normalized_turn_id:
        return False, "missing_run_turn_identity"
    turn = get_turn(conn, normalized_turn_id)
    if turn is None or turn.get("state") not in TURN_OWNER_STATES:
        return False, "turn_not_active"
    row = conn.execute(
        select(
            agent_runs.c.status,
            agent_runs.c.delivery_id,
            message_deliveries.c.session_id,
            message_deliveries.c.state,
            message_deliveries.c.turn_id,
        )
        .select_from(
            agent_runs.join(
                message_deliveries,
                message_deliveries.c.id == agent_runs.c.delivery_id,
            )
        )
        .where(agent_runs.c.id == normalized_run_id)
        .limit(1)
    ).mappings().first()
    if row is None:
        return False, "run_delivery_missing"
    if str(row["session_id"] or "") != str(turn["session_id"] or ""):
        return False, "run_session_mismatch"
    expected_delivery_state = "claimed" if turn["state"] == "starting" else "accepted"
    if (
        str(row["turn_id"] or "") != normalized_turn_id
        or row["state"] != expected_delivery_state
    ):
        return False, "run_not_owned_by_turn"
    if str(row["delivery_id"] or "") != str(turn["initial_delivery_id"] or ""):
        return False, "run_is_steered_participant"
    if str(row["status"] or "").strip().lower() not in {
        "running",
        "processing",
    }:
        return False, "run_not_running"
    if agent_run_ids_for_delivery(conn, {"id": row["delivery_id"]}) != [
        normalized_run_id
    ]:
        return False, "delivery_has_other_runs"
    if turn.get("control_mode") == "replace" and turn.get("control_state") in {
        "pending",
        "interrupting",
        "waiting_terminal",
        "reconciling",
    }:
        successor_turn_id = str(turn.get("control_successor_turn_id") or "")
        successor_delivery_id = str(
            turn.get("control_successor_delivery_id") or ""
        )
        successor = get_turn(conn, successor_turn_id)
        successor_delivery = get_delivery(conn, successor_delivery_id)
        if (
            successor is not None
            and successor["state"] == "waiting"
            and successor["session_id"] == turn["session_id"]
            and successor["initial_delivery_id"] == successor_delivery_id
            and successor_delivery is not None
            and successor_delivery["state"] == "interrupt_waiting"
            and successor_delivery["session_id"] == turn["session_id"]
            and successor_delivery["turn_id"] == successor_turn_id
            and successor_delivery["turn_role"] == "initial"
        ):
            return False, "turn_has_replacement_successor"
        return False, "turn_replacement_unresolved"
    participant_delivery_ids = [
        str(value)
        for value in conn.execute(
            select(message_deliveries.c.id)
            .where(
                or_(
                    and_(
                        message_deliveries.c.turn_id == normalized_turn_id,
                        message_deliveries.c.state.in_(("claimed", "accepted")),
                    ),
                    and_(
                        message_deliveries.c.current_target_turn_id
                        == normalized_turn_id,
                        message_deliveries.c.state.in_(
                            ("pending_steer", "steering", "reconciling_steer")
                        ),
                    ),
                )
            )
            .order_by(message_deliveries.c.turn_position, message_deliveries.c.id)
        ).scalars()
    ]
    if participant_delivery_ids != [str(row["delivery_id"])]:
        return False, "turn_has_other_participants"
    return True, "exclusive_run_owner"


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
        or policy_for(str(delivery["state"])).run_cancel != "retire"
    ):
        return False
    updated = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=(str(delivery["state"]),),
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
        history_event={"kind": "retire", "reason": reason},
    )
    return updated is not None


def retire_for_run_cancellation(
    conn: Connection,
    session_id: str,
    delivery_id: str,
) -> bool:
    """Retire an exact Run input only when its state proves no native side effect."""

    if retire_not_written(
        conn,
        session_id,
        delivery_id,
        reason="agent_run_canceled_before_native_write",
    ):
        return True
    delivery = get_delivery(conn, delivery_id)
    if (
        delivery is None
        or delivery["session_id"] != session_id
        or delivery["state"] != "interrupt_waiting"
    ):
        return False
    successor_turn_id = str(delivery.get("turn_id") or "")
    successor = get_turn(conn, successor_turn_id)
    if (
        successor is None
        or successor["session_id"] != session_id
        or successor["state"] != "terminal"
        or successor["initial_delivery_id"] != delivery_id
        or successor["terminal_outcome"] != "not_written"
        or successor["settled_by"] != "agent_run_canceled"
        or successor["terminal_evidence_kind"] != "replacement_run_canceled"
    ):
        return False
    predecessor = _one(
        conn,
        select(session_turns)
        .where(session_turns.c.session_id == session_id)
        .where(session_turns.c.control_successor_delivery_id == delivery_id)
        .where(session_turns.c.control_successor_turn_id == successor_turn_id)
        .order_by(session_turns.c.created_at.desc(), session_turns.c.id.desc())
        .limit(1),
    )
    retired = cas_delivery(
        conn,
        delivery_id,
        expected_version=int(delivery["version"]),
        expected_states=("interrupt_waiting",),
        values={
            "state": "retired",
            "retired_at": utc_now_iso(),
            "turn_id": None,
            "turn_role": None,
            "turn_position": None,
        },
        history_event={
            "kind": "retire",
            "reason": "replacement_agent_run_canceled",
        },
    )
    if retired is None:
        raise RuntimeError("replacement Run cancellation lost its waiting successor")
    if predecessor is not None:
        predecessor_values: dict[str, Any] = {
            "control_successor_delivery_id": None,
            "control_successor_turn_id": None,
        }
        if predecessor["state"] == "terminal":
            predecessor_values["control_mode"] = None
        elif predecessor.get("control_state") == "pending":
            predecessor_values.update(
                control_state=None,
                control_mode=None,
                control_attempt_id=None,
                control_expected_native_turn_id=None,
                control_receipt_outcome=None,
                control_receipt_json="{}",
            )
        else:
            predecessor_values["control_mode"] = "stop_only"
        unlinked = cas_turn(
            conn,
            str(predecessor["id"]),
            expected_version=int(predecessor["version"]),
            expected_states=(str(predecessor["state"]),),
            values=predecessor_values,
        )
        if unlinked is None:
            raise RuntimeError("replacement Run cancellation lost predecessor unlink")
    return True


def retire_for_archive(conn: Connection, session_id: str) -> dict[str, Any]:
    """Retire only states proven not written; ambiguity remains reconcilable."""

    rows = [
        dict(row)
        for row in conn.execute(
            select(message_deliveries)
            .where(message_deliveries.c.session_id == session_id)
            .where(message_deliveries.c.state.in_(RUN_CANCEL_RETIRE_STATES))
        ).mappings()
    ]
    retired = 0
    retired_delivery_ids: list[str] = []
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
            retired_delivery_ids.append(str(row["id"]))
    return {
        "retired": retired,
        "delivery_ids": retired_delivery_ids,
    }


def set_draft(conn: Connection, session_id: str, text: str | None) -> bool:
    # A clear is a real draft revision too: retaining its timestamp prevents an
    # offline client based on the previous revision from recreating text that a
    # successful send already cleared. Microseconds keep rapid edits distinct.
    now = turn_now_iso()
    result = conn.execute(
        update(agent_sessions)
        .where(agent_sessions.c.id == session_id)
        .values(
            composer_draft_text=text if text and text.strip() else None,
            composer_draft_updated_at=now,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def get_draft_state(conn: Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        select(
            agent_sessions.c.composer_draft_text,
            agent_sessions.c.composer_draft_updated_at,
        ).where(agent_sessions.c.id == session_id)
    ).first()
    if row is None:
        return None
    return {"text": str(row[0] or ""), "updated_at": row[1]}


def get_draft(conn: Connection, session_id: str) -> dict[str, Any] | None:
    state = get_draft_state(conn, session_id)
    if state is None or not state["text"]:
        return None
    return state


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
