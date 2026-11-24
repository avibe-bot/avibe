"""Persistence helpers for browser Web Push subscriptions."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from storage.models import state_meta, web_push_subscriptions


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return "wps_" + secrets.token_urlsafe(16)


# A disabled anonymous grant, never an account or an active install-local grant.
RETIRED_LOCAL = 'retired:setup-network'
_LAST_BADGE_STATE_PREFIX = "web_push.last_successful_badge."


def _last_badge_state_key(endpoint: str) -> str:
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    return f"{_LAST_BADGE_STATE_PREFIX}{digest}"


def get_last_successful_badge(conn: Connection, *, endpoint: str) -> int | None:
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not endpoint:
        return None
    value_json = conn.execute(
        select(state_meta.c.value_json).where(
            state_meta.c.key == _last_badge_state_key(endpoint)
        )
    ).scalar_one_or_none()
    try:
        value = json.loads(value_json) if value_json is not None else None
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clear_last_successful_badges(conn: Connection, endpoints: list[str]) -> None:
    keys = [
        _last_badge_state_key(endpoint)
        for endpoint in dict.fromkeys(endpoints)
        if isinstance(endpoint, str) and endpoint
    ]
    if keys:
        conn.execute(state_meta.delete().where(state_meta.c.key.in_(keys)))


def _set_last_successful_badge(
    conn: Connection,
    *,
    endpoint: str,
    badge_count: int,
    now: str,
) -> None:
    stmt = sqlite_insert(state_meta).values(
        key=_last_badge_state_key(endpoint),
        value_json=json.dumps(badge_count),
        updated_at=now,
    )
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[state_meta.c.key],
            set_={"value_json": json.dumps(badge_count), "updated_at": now},
        )
    )


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
    # A replaced endpoint can no longer authorize another automatic repair.
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.endpoint.in_(endpoints))
        .values(enabled=0, provider_invalidated_at=None, updated_at=now)
    )
    _clear_last_successful_badges(conn, endpoints)


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
            .values(enabled=0, provider_invalidated_at=None, updated_at=now)
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
            # Foreground proof is a NEW grant even at the same endpoint. A sender
            # holding a pre-binding subscription must not inherit that proof.
            'id': case((web_push_subscriptions.c.user_key == RETIRED_LOCAL, stmt.excluded.id),
                       else_=web_push_subscriptions.c.id),
            "user_key": user_key,
            "p256dh": p256dh,
            "auth": auth,
            "device_id": device_id,
            "user_agent": user_agent,
            "device_label": device_label,
            "enabled": 1,
            "last_failure_at": None,
            "provider_invalidated_at": None,
            "failure_count": 0,
            "updated_at": now,
        },
    )
    conn.execute(stmt)
    _clear_last_successful_badges(conn, [endpoint])
    row = conn.execute(
        select(web_push_subscriptions).where(web_push_subscriptions.c.endpoint == endpoint)
    ).mappings().one()
    return _row_to_dict(row)


def upsert_background_rotated_subscription(
    conn: Connection,
    *,
    user_key: str,
    payload: dict[str, Any],
    previous_endpoints: list[str] | None = None,
    user_agent: str | None = None,
) -> dict[str, Any] | None:
    """Accept a service-worker rotation only for an active or provider-failed row."""

    endpoint, _, _ = validate_subscription_payload(payload)
    previous_candidates = _normalize_previous_endpoints(previous_endpoints, endpoint)
    if not previous_candidates:
        return None
    previous = conn.execute(
        select(web_push_subscriptions)
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.endpoint.in_(previous_candidates))
        .where(
            (web_push_subscriptions.c.enabled == 1)
            | web_push_subscriptions.c.provider_invalidated_at.is_not(None)
        )
        .limit(1)
    ).mappings().first()
    if previous is None:
        return None

    current = get_by_endpoint(conn, endpoint=endpoint)
    if current is not None and (current['user_key'] != user_key or (not current['enabled'] and not current.get('provider_invalidated_at'))):
        return None
    return upsert_subscription(
        conn,
        user_key=user_key,
        payload=payload,
        user_agent=user_agent,
        device_id=(
            current.get("device_id")
            if current is not None
            else previous.get("device_id")
        ),
        previous_endpoints=previous_endpoints,
    )


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
    previous_candidates = _normalize_previous_endpoints(previous_endpoints, endpoint)
    recoverable_previous = None
    if previous_candidates:
        previous_rows = conn.execute(
            select(web_push_subscriptions)
            .where(web_push_subscriptions.c.user_key == user_key)
            .where(web_push_subscriptions.c.endpoint.in_(previous_candidates))
            .where(
                (web_push_subscriptions.c.enabled == 1)
                | web_push_subscriptions.c.provider_invalidated_at.is_not(None)
            )
        ).mappings().all()
        for previous_row in previous_rows:
            previous_device_id = previous_row["device_id"]
            if previous_device_id is not None and previous_device_id != device_id:
                continue
            recoverable_previous = previous_row
            break
    existing = get_enabled_by_endpoint(conn, endpoint=endpoint, user_key=user_key)
    if existing is None:
        if (
            get_by_endpoint(conn, endpoint=endpoint) is None
            and recoverable_previous is not None
        ):
            return upsert_subscription(
                conn,
                user_key=user_key,
                payload=payload,
                user_agent=user_agent,
                device_label=device_label,
                device_id=device_id,
                previous_endpoints=previous_endpoints,
            )
        return None
    _disable_previous_endpoints(
        conn,
        user_key=user_key,
        endpoint=endpoint,
        previous_endpoints=previous_endpoints,
        now=now,
    )
    conn.execute(
        web_push_subscriptions.update()
        .where(web_push_subscriptions.c.user_key == user_key)
        .where(web_push_subscriptions.c.device_id == device_id)
        .where(web_push_subscriptions.c.endpoint != endpoint)
        .values(enabled=0, provider_invalidated_at=None, updated_at=now)
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


def belongs_to_signed_account(conn: Connection, payload: dict) -> bool:
    endpoint, _, _ = validate_subscription_payload(payload)
    row = get_by_endpoint(conn, endpoint=endpoint)
    return row is not None and row["user_key"].startswith("remote:")


def is_binding_retired(conn: Connection, endpoint: str) -> bool:
    return get_by_endpoint(conn, endpoint=endpoint, user_key=RETIRED_LOCAL) is not None


def resume_retired_host_subscription(
    conn: Connection,
    *,
    payload: dict,
    device_id: str | None,
    device_label: str | None,
    user_agent: str | None,
    previous_endpoints: list[str] | None = None,
) -> dict | None:
    """Only called for a trusted foreground host while holding the SQLite writer.

    Recover an existing grant, never one that was explicitly turned off.
    A rotated endpoint can name its predecessor only if the new endpoint is
    unused. Never transfer a signed subject's, or another device's, row.
    """
    endpoint, p256dh, auth = validate_subscription_payload(payload)
    current = get_by_endpoint(conn, endpoint=endpoint)
    if current is not None:
        if current["user_key"] != RETIRED_LOCAL or current["p256dh"] != p256dh or current["auth"] != auth:
            return None
        candidates = [current]
    else:
        prior = _normalize_previous_endpoints(previous_endpoints, endpoint)
        if not prior:
            return None
        candidates = (
            conn.execute(
                select(web_push_subscriptions).where(
                    web_push_subscriptions.c.user_key == RETIRED_LOCAL,
                    web_push_subscriptions.c.endpoint.in_(prior),
                )
            )
            .mappings()
            .all()
        )
    match = next((row for row in candidates if current is not None or row["device_id"] in (None, device_id)), None)
    if match is None:
        return None
    device_id = device_id or match["device_id"]
    if match["endpoint"] != endpoint:
        # Old native registrations can lack a browser device ID. Keep the two
        # ends of this rotation associated so a concurrent opt-out naming only
        # the old endpoint also disables the new one, whichever writer won.
        device_id = device_id or match["id"]
        conn.execute(
            web_push_subscriptions.update()
            .where(web_push_subscriptions.c.id == match["id"])
            .values(device_id=device_id)
        )
        disable_subscription(conn, endpoint=match["endpoint"], user_key="local")
    return upsert_subscription(
        conn, user_key="local", payload=payload, device_id=device_id, device_label=device_label, user_agent=user_agent
    )


def disable_subscription(conn: Connection, *, endpoint: str, user_key: str | None = None) -> bool:
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not endpoint:
        return False
    now = _utc_now_iso()
    stmt = web_push_subscriptions.update().where(web_push_subscriptions.c.endpoint == endpoint)
    if user_key == "local":
        stmt = stmt.where(web_push_subscriptions.c.user_key.in_((user_key, RETIRED_LOCAL))).values(user_key="local")
    elif user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    else:
        owner = web_push_subscriptions.c.user_key
        stmt = stmt.values(user_key=case((owner == RETIRED_LOCAL, "local"), else_=owner))
    result = conn.execute(stmt.values(enabled=0, last_failure_at=None, provider_invalidated_at=None, updated_at=now))
    if result.rowcount:
        _clear_last_successful_badges(conn, [endpoint])
    return bool(result.rowcount)


def disable_device_subscription(
    conn: Connection,
    *,
    user_key: str,
    device_id: str | None = None,
    endpoint: str | None = None,
) -> bool:
    """Disable the submitted endpoint and any rotated row for this browser."""

    device_id = device_id.strip() if isinstance(device_id, str) else ""
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    if not device_id and not endpoint:
        return False
    device_ids = {device_id} if device_id else set()
    if endpoint:
        endpoint_row = get_by_endpoint(conn, endpoint=endpoint)
        allowed = (user_key, RETIRED_LOCAL) if user_key == "local" else (user_key,)
        if endpoint_row and endpoint_row["user_key"] in allowed and endpoint_row.get("device_id"):
            # A previous endpoint keeps the rotation's device identity even if
            # another tab submitted a different ID during logout.
            device_ids.add(endpoint_row["device_id"])
    stmt = web_push_subscriptions.update()
    if user_key == "local":
        stmt = stmt.where(web_push_subscriptions.c.user_key.in_((user_key, RETIRED_LOCAL))).values(user_key="local")
    else:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    if endpoint and device_ids:
        stmt = stmt.where(
            (web_push_subscriptions.c.endpoint == endpoint) | (web_push_subscriptions.c.device_id.in_(device_ids))
        )
    elif endpoint:
        stmt = stmt.where(web_push_subscriptions.c.endpoint == endpoint)
    else:
        stmt = stmt.where(web_push_subscriptions.c.device_id.in_(device_ids))
    endpoints = [
        row[0]
        for row in conn.execute(
            select(web_push_subscriptions.c.endpoint).where(stmt.whereclause)
        ).all()
    ]
    result = conn.execute(
        stmt.values(enabled=0, last_failure_at=None, provider_invalidated_at=None, updated_at=_utc_now_iso())
    )
    if result.rowcount:
        _clear_last_successful_badges(conn, endpoints)
    return bool(result.rowcount)


def count_enabled(conn: Connection, *, user_key: str | None = None) -> int:
    stmt = select(func.count()).select_from(web_push_subscriptions).where(web_push_subscriptions.c.enabled == 1)
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    return int(conn.execute(stmt).scalar_one())


_DELIVERY_GENERATION = 'remote_access.push_delivery_generation.v1'


def record_binding_retirement(conn, binding, now):
    stmt = sqlite_insert(state_meta).values(key=_DELIVERY_GENERATION, value_json=json.dumps(binding), updated_at=str(now))
    conn.execute(stmt.on_conflict_do_update(index_elements=['key'], set_={'value_json': stmt.excluded.value_json,
                                                                          'updated_at': stmt.excluded.updated_at}))


def _delivery_generation():
    return func.coalesce(select(state_meta.c.value_json).where(state_meta.c.key == _DELIVERY_GENERATION).scalar_subquery(), '')


def _delivery_rows():
    # One snapshot for the endpoint and the retirement preceding its selection.
    return select(web_push_subscriptions, _delivery_generation().label('_delivery_generation'))


def _same_delivery_generation(subscription):
    if subscription and str(subscription.get('user_key', '')).startswith('remote:'):
        return _delivery_generation() == subscription.get('_delivery_generation')
    return True


def list_enabled(conn: Connection, *, user_key: str | None = None) -> list[dict[str, Any]]:
    from storage.device_functions import delivery_allowed
    stmt = _delivery_rows().where(web_push_subscriptions.c.enabled == 1, delivery_allowed())
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


def get_by_endpoint(
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
    )
    if user_key is not None:
        stmt = stmt.where(web_push_subscriptions.c.user_key == user_key)
    row = conn.execute(stmt).mappings().first()
    return _row_to_dict(row) if row else None


def get_enabled_by_endpoint(
    conn: Connection,
    *,
    endpoint: str,
    user_key: str | None = None,
) -> dict[str, Any] | None:
    row = get_by_endpoint(conn, endpoint=endpoint, user_key=user_key)
    return row if row and row["enabled"] else None


def get_deliverable_by_endpoint(conn: Connection, *, endpoint: str, user_key: str):
    from storage.device_functions import delivery_allowed
    row = conn.execute(_delivery_rows().where(web_push_subscriptions.c.endpoint==endpoint,
        web_push_subscriptions.c.user_key==user_key, web_push_subscriptions.c.enabled==1, delivery_allowed())).mappings().first()
    return _row_to_dict(row) if row else None


def mark_send_success(
    conn: Connection,
    *,
    endpoint: str,
    badge_count: int | None = None,
    subscription: dict | None = None,
) -> None:
    now = _utc_now_iso()
    result = conn.execute(
        web_push_subscriptions.update().where(_same_delivery_generation(subscription))
        .where(web_push_subscriptions.c.endpoint == endpoint)
        .where(web_push_subscriptions.c.enabled == 1)
        .values(last_success_at=now, last_failure_at=None, provider_invalidated_at=None, failure_count=0, updated_at=now)
    )
    if result.rowcount and badge_count is not None:
        if isinstance(badge_count, bool) or not isinstance(badge_count, int) or badge_count < 0:
            raise ValueError("invalid_badge_count")
        _set_last_successful_badge(
            conn,
            endpoint=endpoint,
            badge_count=badge_count,
            now=now,
        )


def mark_send_failure(conn: Connection, *, endpoint: str, disable: bool = False, subscription: dict | None = None) -> None:
    now = _utc_now_iso()
    values = {
        "last_failure_at": now,
        "failure_count": web_push_subscriptions.c.failure_count + 1,
        "updated_at": now,
    }
    if disable:
        values["enabled"] = 0
        values["provider_invalidated_at"] = now
    result = conn.execute(
        web_push_subscriptions.update().where(_same_delivery_generation(subscription))
        .where(web_push_subscriptions.c.endpoint == endpoint)
        .where(web_push_subscriptions.c.enabled == 1)
        .values(**values)
    )
    if result.rowcount and disable:
        _clear_last_successful_badges(conn, [endpoint])
