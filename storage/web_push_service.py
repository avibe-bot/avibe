"""Persistence helpers for browser Web Push subscriptions."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from storage.models import web_push_subscriptions


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return "wps_" + secrets.token_urlsafe(16)


def validate_subscription_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    endpoint = payload.get("endpoint")
    keys = payload.get("keys")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("endpoint_required")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("endpoint_must_be_https")
    if not isinstance(keys, dict):
        raise ValueError("keys_required")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not p256dh.strip():
        raise ValueError("p256dh_required")
    if not isinstance(auth, str) or not auth.strip():
        raise ValueError("auth_required")
    return endpoint.strip(), p256dh.strip(), auth.strip()


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return data


def _normalize_previous_endpoints(previous_endpoints: list[str] | None, current_endpoint: str) -> list[str]:
    if not previous_endpoints:
        return []
    endpoints: list[str] = []
    seen: set[str] = {current_endpoint}
    for endpoint in previous_endpoints:
        if not isinstance(endpoint, str):
            continue
        endpoint = endpoint.strip()
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints


def _disable_previous_endpoints(
    conn: Connection,
    *,
    user_key: str,
    endpoint: str,
    previous_endpoints: list[str] | None,
    now: str,
) -> None:
    endpoints = _normalize_previous_endpoints(previous_endpoints, endpoint)
    if not endpoints:
        return
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.endpoint.in_(endpoints))
        .where(web_push_subscriptions.c.enabled == 1)
        .values(enabled=0, updated_at=now)
    )


def upsert_subscription(
    conn: Connection,
    *,
    user_key: str,
    payload: dict[str, Any],
    user_agent: str | None = None,
    device_label: str | None = None,
    device_id: str | None = None,
    previous_endpoints: list[str] | None = None,
) -> dict[str, Any]:
    endpoint, p256dh, auth = validate_subscription_payload(payload)
    device_id = device_id.strip() if isinstance(device_id, str) else None
    if not device_id:
        device_id = None
    now = _utc_now_iso()
    _disable_previous_endpoints(
        conn,
        user_key=user_key,
        endpoint=endpoint,
        previous_endpoints=previous_endpoints,
        now=now,
    )
    if device_id is not None:
        conn.execute(
            web_push_subscriptions.update()
            .where(web_push_subscriptions.c.user_key == user_key)
            .where(web_push_subscriptions.c.device_id == device_id)
            .where(web_push_subscriptions.c.endpoint != endpoint)
            .where(web_push_subscriptions.c.enabled == 1)
            .values(enabled=0, updated_at=now)
        )
    stmt = sqlite_insert(web_push_subscriptions).values(
        id=_new_id(),
        user_key=user_key,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        device_id=device_id,
        user_agent=user_agent,
        device_label=device_label,
        enabled=1,
        failure_count=0,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[web_push_subscriptions.c.endpoint],
        set_={
            "user_key": user_key,
            "p256dh": p256dh,
            "auth": auth,
            "device_id": device_id,
            "user_agent": user_agent,
            "device_label": device_label,
            "enabled": 1,
            "failure_count": 0,
            "updated_at": now,
        },
    )
    conn.execute(stmt)
    row = conn.execute(
        select(web_push_subscriptions).where(web_push_subscriptions.c.endpoint == endpoint)
    ).mappings().one()
    return _row_to_dict(row)


def attach_device_to_enabled_subscription(
    conn: Connection,
    *,
    user_key: str,
    payload: dict[str, Any],
    user_agent: str | None = None,
    device_label: str | None = None,
    device_id: str | None = None,
    previous_endpoints: list[str] | None = None,
) -> dict[str, Any] | None:
    endpoint, p256dh, auth = validate_subscription_payload(payload)
    device_id = device_id.strip() if isinstance(device_id, str) else None
    if not device_id:
        return get_enabled_by_endpoint(conn, endpoint=endpoint, user_key=user_key)
    now = _utc_now_iso()
    _disable_previous_endpoints(
        conn,
        user_key=user_key,
        endpoint=endpoint,
        previous_endpoints=previous_endpoints,
        now=now,
    )
    existing = get_enabled_by_endpoint(conn, endpoint=endpoint, user_key=user_key)
    if existing is None:
        return None
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.device_id == device_id)
        .where(web_push_subscriptions.c.endpoint != endpoint)
        .where(web_push_subscriptions.c.enabled == 1)
        .values(enabled=0, updated_at=now)
    )
    values = {
        "p256dh": p256dh,
        "auth": auth,
        "device_id": device_id,
        "user_agent": user_agent,
        "updated_at": now,
    }
    if device_label is not None:
        values["device_label"] = device_label
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.endpoint == endpoint)
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.enabled == 1)
        .values(**values)
    )
    return get_enabled_by_endpoint(conn, endpoint=endpoint, user_key=user_key)


def disable_subscription(conn: Connection, *, endpoint: str, user_key: str | None = None) -> bool:
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not endpoint:
        return False
    now = _utc_now_iso()
    stmt = web_push_subscriptions.update().where(web_push_subscriptions.c.endpoint == endpoint)
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    result = conn.execute(
        stmt.values(enabled=0, updated_at=now)
    )
    return bool(result.rowcount)


def disable_device_subscription(
    conn: Connection,
    *,
    user_key: str,
    device_id: str | None = None,
    endpoint: str | None = None,
) -> bool:
    """Disable only the logging-out browser's selected Push subscription."""

    device_id = device_id.strip() if isinstance(device_id, str) else ""
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not device_id and not endpoint:
        return False
    stmt = (
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.enabled == 1)
    )
    if endpoint:
        stmt = stmt.where(web_push_subscriptions.c.endpoint == endpoint)
    else:
        stmt = stmt.where(web_push_subscriptions.c.device_id == device_id)
    result = conn.execute(
        stmt.values(enabled=0, updated_at=_utc_now_iso())
    )
    return bool(result.rowcount)


def count_enabled(conn: Connection, *, user_key: str | None = None) -> int:
    stmt = select(func.count()).select_from(web_push_subscriptions).where(web_push_subscriptions.c.enabled == 1)
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    return int(conn.execute(stmt).scalar_one())


def list_enabled(conn: Connection, *, user_key: str | None = None) -> list[dict[str, Any]]:
    stmt = select(web_push_subscriptions).where(web_push_subscriptions.c.enabled == 1)
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    rows = conn.execute(stmt).mappings().all()
    return [_row_to_dict(row) for row in rows]


def has_enabled_user_key(conn: Connection, *, user_key: str) -> bool:
    if not isinstance(user_key, str) or not user_key.strip():
        return False
    row = conn.execute(
        select(web_push_subscriptions.c.id)
        .where(web_push_subscriptions.c.enabled == 1)
        .where(web_push_subscriptions.c.user_key == user_key.strip())
        .limit(1)
    ).first()
    return row is not None


def get_enabled_by_endpoint(
    conn: Connection,
    *,
    endpoint: str,
    user_key: str | None = None,
) -> dict[str, Any] | None:
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not endpoint:
        return None
    stmt = (
        select(web_push_subscriptions)
        .where(web_push_subscriptions.c.endpoint == endpoint)
        .where(web_push_subscriptions.c.enabled == 1)
    )
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    row = conn.execute(stmt).mappings().first()
    return _row_to_dict(row) if row else None


def mark_send_success(conn: Connection, *, endpoint: str) -> None:
    now = _utc_now_iso()
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.endpoint == endpoint)
        .values(last_success_at=now, last_failure_at=None, failure_count=0, updated_at=now)
    )


def mark_send_failure(conn: Connection, *, endpoint: str, disable: bool = False) -> None:
    now = _utc_now_iso()
    values = {
        "last_failure_at": now,
        "failure_count": web_push_subscriptions.c.failure_count + 1,
        "updated_at": now,
    }
    if disable:
        values["enabled"] = 0
    conn.execute(
        web_push_subscriptions.update().where(web_push_subscriptions.c.endpoint == endpoint).values(**values)
    )
