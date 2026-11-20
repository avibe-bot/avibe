"""Durable Agent-to-device system schedule operations and device-bound attempts."""

from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, select

from vibe.i18n import t

from storage.db import get_cached_sqlite_engine, retry_sqlite_write
from storage.models import (
    agent_sessions,
    system_schedule_attempts,
    system_schedule_operations,
)

CONTRACT_VERSION = 1
DEFAULT_REQUEST_TTL_SECONDS = 300
MIN_REQUEST_TTL_SECONDS = 30
MAX_REQUEST_TTL_SECONDS = 900
MAX_IDENTIFIER_LENGTH = 200
MAX_TITLE_LENGTH = 200
MAX_TIME_ZONE_LENGTH = 100
MAX_EXCLUDED_LEASES = 64

_NATIVE_TERMINAL_STATES = frozenset(
    {
        "scheduled",
        "user_denied",
        "permission_denied",
        "unsupported",
        "failed",
        "expired",
        "stale_context",
        "cancelled",
    }
)
_AUTHORIZATIONS = frozenset(
    {"not_determined", "authorized", "denied", "restricted", "unsupported"}
)
_ACTIVE_CLAIM_STATES = frozenset({"pending", "claimed"})


class SystemScheduleServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class ParsedReceipt:
    canonical_json: str
    payload: dict[str, Any]


def _ensure_state() -> None:
    from storage.importer import ensure_sqlite_state

    ensure_sqlite_state()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SystemScheduleServiceError(
            "invalid_request",
            f"{field} must be an ISO-8601 timestamp with an offset",
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemScheduleServiceError(
            "invalid_request",
            f"{field} must be an ISO-8601 timestamp with an offset",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemScheduleServiceError(
            "invalid_request",
            f"{field} must include a UTC offset",
        )
    return parsed.astimezone(timezone.utc)


def _bounded_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise SystemScheduleServiceError("invalid_request", f"{field} is required")
    result = value.strip()
    if (
        not result
        or len(result) > MAX_IDENTIFIER_LENGTH
        or any(
            unicodedata.category(character) in {"Cc", "Cf"}
            for character in result
        )
    ):
        raise SystemScheduleServiceError("invalid_request", f"{field} is invalid")
    return result


def _excluded_lease_tokens(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SystemScheduleServiceError(
            "invalid_request",
            "exclude_lease_tokens must be an array",
        )
    if len(value) > MAX_EXCLUDED_LEASES:
        raise SystemScheduleServiceError(
            "invalid_request",
            f"exclude_lease_tokens may contain at most {MAX_EXCLUDED_LEASES} leases",
        )
    result = tuple(
        _bounded_identifier(item, field="exclude_lease_tokens") for item in value
    )
    if len(set(result)) != len(result):
        raise SystemScheduleServiceError(
            "invalid_request",
            "exclude_lease_tokens must not contain duplicates",
        )
    return result


def _title(value: object) -> str:
    if not isinstance(value, str):
        raise SystemScheduleServiceError("invalid_request", "title is required")
    result = value.strip()
    if not result or len(result) > MAX_TITLE_LENGTH:
        raise SystemScheduleServiceError(
            "invalid_request",
            "title must contain 1 to 200 characters",
        )
    return result


def _time_zone(value: object) -> str:
    if not isinstance(value, str):
        raise SystemScheduleServiceError("invalid_request", "time_zone is required")
    result = value.strip()
    if not result or len(result) > MAX_TIME_ZONE_LENGTH:
        raise SystemScheduleServiceError("invalid_request", "time_zone is invalid")
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise SystemScheduleServiceError(
            "invalid_request",
            "time_zone must be an IANA time zone",
        ) from exc
    return result


def _kind(value: object) -> str:
    if value not in {"alarm", "reminder"}:
        raise SystemScheduleServiceError(
            "invalid_request",
            "kind must be alarm or reminder",
        )
    return str(value)


def _ttl(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SystemScheduleServiceError(
            "invalid_request",
            "expires_in must be an integer number of seconds",
        )
    if value < MIN_REQUEST_TTL_SECONDS or value > MAX_REQUEST_TTL_SECONDS:
        raise SystemScheduleServiceError(
            "invalid_request",
            f"expires_in must be between {MIN_REQUEST_TTL_SECONDS} and "
            f"{MAX_REQUEST_TTL_SECONDS} seconds",
        )
    return value


def _new_request_id() -> str:
    return "ssr_" + secrets.token_urlsafe(18)


def _attempt_key(operation_id: str, action: str, generation: int) -> str:
    """Encode the semantic attempt identity into one DB-unique column."""
    return json.dumps(
        [operation_id, action, generation],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _new_lease_token() -> str:
    return "ssl_" + secrets.token_urlsafe(24)


def _receipt_value(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_operation(conn, operation_id: str) -> Mapping[str, Any] | None:
    return conn.execute(
        select(system_schedule_operations).where(
            system_schedule_operations.c.operation_id == operation_id
        )
    ).mappings().one_or_none()


def _load_attempt(conn, request_id: str) -> Mapping[str, Any] | None:
    return conn.execute(
        select(system_schedule_attempts).where(
            system_schedule_attempts.c.request_id == request_id
        )
    ).mappings().one_or_none()


def _attempts(conn, operation_id: str) -> list[Mapping[str, Any]]:
    return list(
        conn.execute(
            select(system_schedule_attempts)
            .where(system_schedule_attempts.c.operation_id == operation_id)
            .order_by(
                system_schedule_attempts.c.action,
                system_schedule_attempts.c.generation,
            )
        ).mappings()
    )


def _operation_for_session(
    conn,
    operation_id: str,
    session_id: str,
    instance_id: str,
) -> Mapping[str, Any]:
    row = _load_operation(conn, operation_id)
    if (
        row is None
        or row["session_id"] != session_id
        or row["instance_id"] != instance_id
    ):
        raise SystemScheduleServiceError(
            "system_schedule_not_found",
            "No system schedule operation with that ID belongs to this Agent Session.",
            details={"operation_id": operation_id},
        )
    return row


def _attempt_public(row: Mapping[str, Any]) -> dict[str, Any]:
    state = row["terminal_state"] or row["claim_state"]
    return {
        "action": row["action"],
        "generation": row["generation"],
        "purpose": row["purpose"],
        "state": state,
        "claim_state": row["claim_state"],
        "terminal_state": row["terminal_state"],
        "request_id": row["request_id"],
        "expires_at": row["deadline"],
        "receipt": _receipt_value(row["receipt_json"]),
    }


def _operation_public(conn, row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    attempts = _attempts(conn, row["operation_id"])
    create = next(item for item in attempts if item["action"] == "create")
    cancels = [item for item in attempts if item["action"] == "cancel"]
    user_cancels = [item for item in cancels if item["purpose"] == "user"]
    active_cancels = [
        item
        for item in cancels
        if item["claim_state"] in _ACTIVE_CLAIM_STATES
        and item["terminal_state"] is None
    ]
    user_cancelled = any(
        item["terminal_state"] == "cancelled" for item in user_cancels
    )
    if user_cancelled:
        state = "cancelled"
    elif create["terminal_state"] is not None:
        state = str(create["terminal_state"])
        if state == "scheduled" and active_cancels:
            state = "cancelling"
    elif create["claim_state"] == "claimed" and create["deadline"] <= _iso(now):
        state = "awaiting_device"
    else:
        state = "pending"
    status_message = (
        t("systemSchedule.waitingForDevice")
        if state == "awaiting_device"
        else None
    )
    if row.get('blocked_by_schedule_gate') and row['bound_device_id'] is None and state in ('pending','expired'):
        status_message = 'Alarms and reminders were turned off on an eligible device. Ask the user to enable Alarms and reminders in Devices before proposing another.'
    return {
        "contract": CONTRACT_VERSION,
        "operation_id": row["operation_id"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "title": row["title"],
        "fire_at": row["fire_at"],
        "time_zone": row["time_zone"],
        "expires_at": create["deadline"],
        "instance_id": row["instance_id"],
        "device_id": row["bound_device_id"],
        "state": state,
        "status_message": status_message,
        "create": _attempt_public(create),
        "cancel": _attempt_public(user_cancels[-1]) if user_cancels else None,
        "cleanup": None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _advance_pending_deadlines(conn, now: datetime) -> None:
    now_text = _iso(now)
    for action in ("create", "cancel"):
        conn.execute(
            system_schedule_attempts.update()
            .where(system_schedule_attempts.c.action == action)
            .where(system_schedule_attempts.c.claim_state == "pending")
            .where(system_schedule_attempts.c.terminal_state.is_(None))
            .where(system_schedule_attempts.c.deadline <= now_text)
            .values(
                claim_state="settled",
                terminal_state="expired",
                updated_at=now_text,
            )
        )


def _next_cancel_generation(conn, operation_id: str) -> int:
    current = conn.execute(
        select(func.max(system_schedule_attempts.c.generation)).where(
            and_(
                system_schedule_attempts.c.operation_id == operation_id,
                system_schedule_attempts.c.action == "cancel",
            )
        )
    ).scalar_one()
    return int(current or 0) + 1


def _insert_cancel_attempt(
    conn,
    operation: Mapping[str, Any],
    *,
    expires_in: int,
    now: datetime,
) -> Mapping[str, Any]:
    device_id = operation["bound_device_id"]
    if not device_id:
        raise SystemScheduleServiceError(
            "device_not_bound",
            "The system schedule operation has not been claimed by a device.",
        )
    generation = _next_cancel_generation(conn, operation["operation_id"])
    request_id = _new_request_id()
    timestamp = _iso(now)
    conn.execute(
        system_schedule_attempts.insert().values(
            operation_id=operation["operation_id"],
            action="cancel",
            generation=generation,
            attempt_key=_attempt_key(
                operation["operation_id"], "cancel", generation
            ),
            purpose="user",
            request_id=request_id,
            instance_id=operation["instance_id"],
            device_id=device_id,
            lease_token=None,
            claim_state="pending",
            terminal_state=None,
            deadline=_iso(now + timedelta(seconds=expires_in)),
            receipt_json=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    return _load_attempt(conn, request_id)


def _active_cancel(conn, operation_id: str) -> Mapping[str, Any] | None:
    return conn.execute(
        select(system_schedule_attempts)
        .where(system_schedule_attempts.c.operation_id == operation_id)
        .where(system_schedule_attempts.c.action == "cancel")
        .where(system_schedule_attempts.c.claim_state.in_(("pending", "claimed")))
        .where(system_schedule_attempts.c.terminal_state.is_(None))
        .order_by(system_schedule_attempts.c.generation.desc())
        .limit(1)
    ).mappings().one_or_none()


def _successful_cancel_exists(conn, operation_id: str) -> bool:
    return bool(
        conn.execute(
            select(system_schedule_attempts.c.request_id)
            .where(system_schedule_attempts.c.operation_id == operation_id)
            .where(system_schedule_attempts.c.action == "cancel")
            .where(system_schedule_attempts.c.terminal_state == "cancelled")
            .limit(1)
        ).scalar_one_or_none()
    )


def create_operation(
    *,
    session_id: str,
    instance_id: str,
    operation_id: str,
    kind: str,
    title: str,
    fire_at: str,
    time_zone: str,
    expires_in: int = DEFAULT_REQUEST_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    session_id = _bounded_identifier(session_id, field="session_id")
    instance_id = _bounded_identifier(instance_id, field="instance_id")
    operation_id = _bounded_identifier(operation_id, field="operation_id")
    kind = _kind(kind)
    title = _title(title)
    fire = _parse_instant(fire_at, field="fire_at")
    time_zone = _time_zone(time_zone)
    expires_in = _ttl(expires_in)
    now = (now or _utc_now()).astimezone(timezone.utc)
    immutable = {
        "session_id": session_id,
        "instance_id": instance_id,
        "kind": kind,
        "title": title,
        "fire_at": _iso(fire),
        "time_zone": time_zone,
    }

    _ensure_state()
    engine = get_cached_sqlite_engine()

    def write() -> dict[str, Any]:
        with engine.begin() as conn:
            _advance_pending_deadlines(conn, now)
            existing = _load_operation(conn, operation_id)
            if existing is not None:
                if any(existing[field] != value for field, value in immutable.items()):
                    raise SystemScheduleServiceError(
                        "operation_conflict",
                        "The operation ID is already bound to a different system "
                        "schedule request.",
                        details={"operation_id": operation_id},
                    )
                return _operation_public(conn, existing, now)
            if fire <= now:
                raise SystemScheduleServiceError(
                    "fire_time_in_past",
                    "The requested time has already passed.",
                )
            session = conn.execute(
                select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
            if session != "active":
                raise SystemScheduleServiceError(
                    "session_not_found",
                    "The caller Agent Session is missing or archived.",
                    details={"session_id": session_id},
                )
            timestamp = _iso(now)
            conn.execute(
                system_schedule_operations.insert().values(
                    operation_id=operation_id,
                    schema_version=CONTRACT_VERSION,
                    **immutable,
                    bound_device_id=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            conn.execute(
                system_schedule_attempts.insert().values(
                    operation_id=operation_id,
                    action="create",
                    generation=1,
                    attempt_key=_attempt_key(operation_id, "create", 1),
                    purpose="user",
                    request_id=_new_request_id(),
                    instance_id=instance_id,
                    device_id=None,
                    lease_token=None,
                    claim_state="pending",
                    terminal_state=None,
                    deadline=_iso(now + timedelta(seconds=expires_in)),
                    receipt_json=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            return _operation_public(conn, _load_operation(conn, operation_id), now)

    return retry_sqlite_write(write)


def get_operation(
    *,
    session_id: str,
    instance_id: str,
    operation_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    session_id = _bounded_identifier(session_id, field="session_id")
    instance_id = _bounded_identifier(instance_id, field="instance_id")
    operation_id = _bounded_identifier(operation_id, field="operation_id")
    now = (now or _utc_now()).astimezone(timezone.utc)
    _ensure_state()
    engine = get_cached_sqlite_engine()

    def write() -> dict[str, Any]:
        with engine.begin() as conn:
            _advance_pending_deadlines(conn, now)
            row = _operation_for_session(
                conn,
                operation_id,
                session_id,
                instance_id,
            )
            return _operation_public(conn, row, now)

    return retry_sqlite_write(write)


def cancel_operation(
    *,
    session_id: str,
    instance_id: str,
    operation_id: str,
    expires_in: int = DEFAULT_REQUEST_TTL_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    session_id = _bounded_identifier(session_id, field="session_id")
    instance_id = _bounded_identifier(instance_id, field="instance_id")
    operation_id = _bounded_identifier(operation_id, field="operation_id")
    expires_in = _ttl(expires_in)
    now = (now or _utc_now()).astimezone(timezone.utc)
    _ensure_state()
    engine = get_cached_sqlite_engine()

    def write() -> dict[str, Any]:
        with engine.begin() as conn:
            _advance_pending_deadlines(conn, now)
            operation = _operation_for_session(
                conn,
                operation_id,
                session_id,
                instance_id,
            )
            create = conn.execute(
                select(system_schedule_attempts).where(
                    and_(
                        system_schedule_attempts.c.operation_id == operation_id,
                        system_schedule_attempts.c.action == "create",
                    )
                )
            ).mappings().one()
            if create["terminal_state"] == "cancelled" and _successful_cancel_exists(
                conn, operation_id
            ):
                return _operation_public(conn, operation, now)
            if create["claim_state"] == "pending" and create["terminal_state"] is None:
                conn.execute(
                    system_schedule_attempts.update()
                    .where(system_schedule_attempts.c.request_id == create["request_id"])
                    .values(
                        claim_state="settled",
                        terminal_state="cancelled",
                        updated_at=_iso(now),
                    )
                )
                return _operation_public(conn, operation, now)

            if create["claim_state"] == "claimed" and create["terminal_state"] is None:
                conn.execute(
                    system_schedule_attempts.update()
                    .where(system_schedule_attempts.c.request_id == create["request_id"])
                    .values(terminal_state="cancelled", updated_at=_iso(now))
                )
            receipt = _receipt_value(create["receipt_json"])
            system_identifier = (receipt or {}).get("systemIdentifier")
            should_cancel_device = (
                create["claim_state"] == "claimed"
                or create["terminal_state"] == "scheduled"
                or bool(system_identifier)
            )
            if should_cancel_device and _active_cancel(conn, operation_id) is None:
                if not _successful_cancel_exists(conn, operation_id):
                    _insert_cancel_attempt(
                        conn,
                        operation,
                        expires_in=expires_in,
                        now=now,
                    )
            return _operation_public(conn, _load_operation(conn, operation_id), now)

    return retry_sqlite_write(write)


def _claim_payload(
    operation: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    if attempt["action"] == "create":
        request = {
            "requestId": attempt["request_id"],
            "operationId": operation["operation_id"],
            "kind": operation["kind"],
            "title": operation["title"],
            "fireAt": operation["fire_at"],
            "timeZone": operation["time_zone"],
            "recurrence": None,
            "instanceId": operation["instance_id"],
            "sessionId": operation["session_id"],
            "expiresAt": attempt["deadline"],
        }
    else:
        request = {
            "requestId": attempt["request_id"],
            "operationId": operation["operation_id"],
            "kind": operation["kind"],
            "purpose": attempt["purpose"],
        }
    return {
        "action": attempt["action"],
        "generation": attempt["generation"],
        "purpose": attempt["purpose"],
        "leaseToken": attempt["lease_token"],
        "leaseExpiresAt": attempt["deadline"],
        "request": request,
    }


def _sticky_attempt(
    conn,
    *,
    action: str,
    instance_id: str,
    device_id: str,
    excluded: tuple[str, ...],
) -> Mapping[str, Any] | None:
    query = (
        select(system_schedule_attempts)
        .where(system_schedule_attempts.c.action == action)
        .where(system_schedule_attempts.c.claim_state == "claimed")
        .where(system_schedule_attempts.c.instance_id == instance_id)
        .where(system_schedule_attempts.c.device_id == device_id)
        .where(system_schedule_attempts.c.terminal_state.is_(None))
        .where(system_schedule_attempts.c.receipt_json.is_(None))
    )
    if action == "cancel":
        query = query.where(system_schedule_attempts.c.purpose == "user")
    if excluded:
        query = query.where(system_schedule_attempts.c.lease_token.not_in(excluded))
    return conn.execute(
        query.order_by(
            system_schedule_attempts.c.updated_at,
            system_schedule_attempts.c.request_id,
        ).limit(1)
    ).mappings().one_or_none()


def _pending_attempt(
    conn,
    *,
    action: str,
    instance_id: str,
    device_id: str | None,
    kinds: tuple[str, ...] | None = None,
    claimant=None,
) -> Mapping[str, Any] | None:
    query = (
        select(system_schedule_attempts)
        .where(system_schedule_attempts.c.action == action)
        .where(system_schedule_attempts.c.claim_state == "pending")
        .where(system_schedule_attempts.c.terminal_state.is_(None))
        .where(system_schedule_attempts.c.instance_id == instance_id)
    )
    if action == "create" and device_id is None and kinds is not None:
        if not kinds:
            return None
        query = query.where(
            select(system_schedule_operations.c.operation_id)
            .where(
                system_schedule_operations.c.operation_id == system_schedule_attempts.c.operation_id,
                system_schedule_operations.c.kind.in_(kinds),
            )
            .exists()
        )
    if action == "cancel":
        query = query.where(system_schedule_attempts.c.purpose == "user")
    if device_id is None:
        query = query.where(system_schedule_attempts.c.device_id.is_(None))
    else:
        query = query.where(system_schedule_attempts.c.device_id == device_id)
    ordered = query.order_by(system_schedule_attempts.c.created_at, system_schedule_attempts.c.request_id)
    if claimant is not None and action == "create" and device_id is None:
        from storage.workbench_sessions_service import get_active_session

        # Read authorization and first lease share this transaction. An invisible
        # queue head must not block the phone's next eligible proposal.
        for row in conn.execute(ordered).mappings():
            operation = _load_operation(conn, row["operation_id"])
            try:
                get_active_session(conn, operation["session_id"], authorization_context=claimant)
            except LookupError:
                continue
            return row
        return None
    return conn.execute(ordered.limit(1)).mappings().one_or_none()


def claim_next(
    *,
    instance_id: str,
    device_id: str,
    exclude_lease_tokens: object = None,
    allow_create: bool = True,
    kinds: object = None,
    claimant=None,
    device_viewer=None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    instance_id = _bounded_identifier(instance_id, field="instance_id")
    device_id = _bounded_identifier(device_id, field="device_id")
    if not isinstance(allow_create, bool):
        raise SystemScheduleServiceError(
            "invalid_allow_create",
            "allow_create must be a boolean",
        )
    if kinds is not None and (
        not isinstance(kinds, list)
        or any(not isinstance(kind, str) or kind not in {"alarm", "reminder"} for kind in kinds)
        or len(set(kinds)) != len(kinds)
    ):
        raise SystemScheduleServiceError("invalid_kinds", "kinds must contain distinct supported kinds")
    allowed_kinds = tuple(kinds) if kinds is not None else None
    excluded = _excluded_lease_tokens(exclude_lease_tokens)
    now = (now or _utc_now()).astimezone(timezone.utc)
    _ensure_state()
    engine = get_cached_sqlite_engine()

    def write() -> dict[str, Any] | None:
        with engine.begin() as conn:
            _advance_pending_deadlines(conn, now)
            registration, gate = None, True
            if device_viewer and device_viewer[3]:
                from storage.device_functions import schedule_claimant
                registration, gate = schedule_claimant(conn, device_viewer, int(now.timestamp()))
            attempt = _sticky_attempt(
                conn,
                action="cancel",
                instance_id=instance_id,
                device_id=device_id,
                excluded=excluded,
            )
            if attempt is None:
                attempt = _pending_attempt(
                    conn,
                    action="cancel",
                    instance_id=instance_id,
                    device_id=device_id,
                )
            if attempt is None and allow_create:
                attempt = _sticky_attempt(
                    conn,
                    action="create",
                    instance_id=instance_id,
                    device_id=device_id,
                    excluded=excluded,
                )
            if attempt is None and allow_create:
                attempt = _pending_attempt(
                    conn,
                    action="create",
                    instance_id=instance_id,
                    device_id=None,
                    kinds=allowed_kinds,
                    claimant=claimant,
                )
                if attempt and not gate:
                    conn.execute(system_schedule_operations.update().where(system_schedule_operations.c.operation_id==attempt['operation_id'],
                        system_schedule_operations.c.blocked_by_schedule_gate.is_(None)).values(blocked_by_schedule_gate=1))
                    return None
            if attempt is None:
                return None
            operation = _load_operation(conn, attempt["operation_id"])
            if attempt["claim_state"] == "pending":
                token = _new_lease_token()
                values: dict[str, Any] = {
                    "claim_state": "claimed",
                    "lease_token": token,
                    "updated_at": _iso(now),
                }
                if attempt["action"] == "create":
                    values["device_id"] = device_id
                    conn.execute(
                        system_schedule_operations.update()
                        .where(
                            system_schedule_operations.c.operation_id
                            == operation["operation_id"]
                        )
                        .where(system_schedule_operations.c.bound_device_id.is_(None))
                        .values(bound_device_id=device_id, updated_at=_iso(now),blocked_by_schedule_gate=None,
                                registry_activity_id=secrets.randbelow(2**62)+1 if registration else None, **(registration or {}))
                    )
                conn.execute(
                    system_schedule_attempts.update()
                    .where(
                        system_schedule_attempts.c.request_id == attempt["request_id"]
                    )
                    .where(system_schedule_attempts.c.claim_state == "pending")
                    .values(**values)
                )
                attempt = _load_attempt(conn, attempt["request_id"])
                operation = _load_operation(conn, attempt["operation_id"])
            return _claim_payload(operation, attempt)

    return retry_sqlite_write(write)


def _canonical_receipt(receipt: object) -> ParsedReceipt:
    if not isinstance(receipt, dict):
        raise SystemScheduleServiceError(
            "invalid_receipt",
            "receipt must be an object",
        )
    expected = {
        "contract",
        "requestId",
        "operationId",
        "kind",
        "state",
        "systemIdentifier",
        "fireAt",
        "timeZone",
        "authorization",
        "error",
    }
    if set(receipt) != expected or receipt.get("contract") != CONTRACT_VERSION:
        raise SystemScheduleServiceError(
            "invalid_receipt",
            "receipt does not match contract 1",
        )
    request_id = _bounded_identifier(receipt.get("requestId"), field="requestId")
    operation_id = _bounded_identifier(
        receipt.get("operationId"),
        field="operationId",
    )
    kind = _kind(receipt.get("kind"))
    state = receipt.get("state")
    if state not in _NATIVE_TERMINAL_STATES:
        raise SystemScheduleServiceError(
            "invalid_receipt",
            "receipt state is not terminal",
        )
    authorization = receipt.get("authorization")
    if authorization not in _AUTHORIZATIONS:
        raise SystemScheduleServiceError(
            "invalid_receipt",
            "receipt authorization is invalid",
        )
    system_identifier = receipt.get("systemIdentifier")
    if system_identifier is not None:
        system_identifier = _bounded_identifier(
            system_identifier,
            field="systemIdentifier",
        )
    if state == "scheduled" and system_identifier is None:
        raise SystemScheduleServiceError(
            "invalid_receipt",
            "a scheduled receipt needs a system identifier",
        )
    fire_at = receipt.get("fireAt")
    if fire_at is not None:
        fire_at = _iso(_parse_instant(fire_at, field="fireAt"))
    time_zone = receipt.get("timeZone")
    if time_zone is not None:
        time_zone = _time_zone(time_zone)
    error = receipt.get("error")
    if error is not None:
        if (
            not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or not isinstance(error.get("code"), str)
            or not error["code"].strip()
            or not isinstance(error.get("message"), str)
            or not error["message"].strip()
        ):
            raise SystemScheduleServiceError(
                "invalid_receipt",
                "receipt error is invalid",
            )
        error = {
            "code": error["code"].strip(),
            "message": error["message"].strip(),
        }
    normalized = {
        "contract": CONTRACT_VERSION,
        "requestId": request_id,
        "operationId": operation_id,
        "kind": kind,
        "state": state,
        "systemIdentifier": system_identifier,
        "fireAt": fire_at,
        "timeZone": time_zone,
        "authorization": authorization,
        "error": error,
    }
    return ParsedReceipt(
        canonical_json=json.dumps(normalized, separators=(",", ":"), sort_keys=True),
        payload=normalized,
    )


def record_receipt(
    *,
    instance_id: str,
    device_id: str,
    lease_token: str,
    receipt: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    instance_id = _bounded_identifier(instance_id, field="instance_id")
    device_id = _bounded_identifier(device_id, field="device_id")
    lease_token = _bounded_identifier(lease_token, field="lease_token")
    parsed = _canonical_receipt(receipt)
    now = (now or _utc_now()).astimezone(timezone.utc)
    _ensure_state()
    engine = get_cached_sqlite_engine()

    def write() -> dict[str, Any]:
        with engine.begin() as conn:
            _advance_pending_deadlines(conn, now)
            attempt = _load_attempt(conn, parsed.payload["requestId"])
            operation = _load_operation(conn, parsed.payload["operationId"])
            if (
                attempt is None
                or operation is None
                or attempt["operation_id"] != operation["operation_id"]
                or attempt["instance_id"] != instance_id
                or operation["instance_id"] != instance_id
                or attempt["device_id"] != device_id
                or attempt["lease_token"] != lease_token
                or operation["bound_device_id"] != device_id
            ):
                raise SystemScheduleServiceError(
                    "receipt_mismatch",
                    "The receipt does not belong to this device lease.",
                )
            if operation["kind"] != parsed.payload["kind"]:
                raise SystemScheduleServiceError(
                    "receipt_mismatch",
                    "The receipt kind does not match the operation.",
                )
            if parsed.payload["fireAt"] not in {None, operation["fire_at"]}:
                raise SystemScheduleServiceError(
                    "receipt_mismatch",
                    "The receipt fire time does not match the operation.",
                )
            if parsed.payload["timeZone"] not in {None, operation["time_zone"]}:
                raise SystemScheduleServiceError(
                    "receipt_mismatch",
                    "The receipt time zone does not match the operation.",
                )
            if attempt["action"] == "create":
                if parsed.payload["fireAt"] != operation["fire_at"]:
                    raise SystemScheduleServiceError(
                        "receipt_mismatch",
                        "A create receipt must include the operation fire time.",
                    )
                if parsed.payload["timeZone"] != operation["time_zone"]:
                    raise SystemScheduleServiceError(
                        "receipt_mismatch",
                        "A create receipt must include the operation time zone.",
                    )
            elif parsed.payload["state"] in {"scheduled", "user_denied"}:
                raise SystemScheduleServiceError(
                    "invalid_receipt",
                    "A cancel receipt has an invalid state.",
                )
            existing_json = attempt["receipt_json"]
            if existing_json is not None:
                if existing_json != parsed.canonical_json:
                    raise SystemScheduleServiceError(
                        "receipt_conflict",
                        "A different receipt is already stored for this attempt.",
                    )
                return {
                    "action": attempt["action"],
                    "generation": attempt["generation"],
                    "operation": _operation_public(conn, operation, now),
                }
            if attempt["claim_state"] != "claimed":
                digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()[:12]
                raise SystemScheduleServiceError(
                    "stale_receipt",
                    "The device lease is no longer active.",
                    details={"lease_fingerprint": digest},
                )
            terminal = attempt["terminal_state"] or parsed.payload["state"]
            conn.execute(
                system_schedule_attempts.update()
                .where(system_schedule_attempts.c.request_id == attempt["request_id"])
                .values(
                    claim_state="settled",
                    terminal_state=terminal,
                    receipt_json=parsed.canonical_json,
                    updated_at=_iso(now),
                )
            )
            updated = _load_operation(conn, operation["operation_id"])
            return {
                "action": attempt["action"],
                "generation": attempt["generation"],
                "operation": _operation_public(conn, updated, now),
            }

    return retry_sqlite_write(write)
