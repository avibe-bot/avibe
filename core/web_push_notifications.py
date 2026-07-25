"""Notification dispatch from durable Workbench inbox events to Web Push."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from sqlalchemy import or_, select

from core.backend_failure import is_backend_failure_notification
from storage import messages_service, web_push_service
from storage.models import agent_sessions, messages
from vibe.authorization import AuthorizationContext, context_from_session_payload

logger = logging.getLogger(__name__)

_NOTIFIABLE_TYPES = {"result", "error", "notify"}
_UNREAD_GATED_TYPES = {"result"}
WEB_PUSH_NOTIFICATION_DELAY_SECONDS = 3.0
WEB_PUSH_USER_KEY_METADATA = "_web_push_user_key"
WEB_PUSH_USER_KEYS_METADATA = "_web_push_user_keys"
WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA = "_web_push_authorization_contexts"


def _is_notifiable_message(message_type: Any, metadata: Any = None) -> bool:
    normalized_type = str(message_type or "").strip()
    return normalized_type in {"result", "error"} or is_backend_failure_notification(
        normalized_type,
        metadata,
    )


def _parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def maybe_notify_inbox_message(message: dict[str, Any] | None, inbox_row: dict[str, Any] | None) -> None:
    """Schedule Web Push for a newly persisted inbox-visible Workbench message.

    Called after the message row and inbox row exist in the same durable write
    path. Sending happens on a background thread with its own SQLite connection
    so a slow push service never blocks message persistence or SSE fan-out.
    """

    if not message or not inbox_row:
        return
    if message.get("platform") != "avibe":
        return
    if message.get("author") != "agent":
        return
    if not _is_notifiable_message(message.get("type"), message.get("metadata")):
        return
    if not message.get("session_id"):
        return

    # ``badge_count`` is computed per subscription owner at send time, not from
    # this one session's unread count. That keeps it current after the debounce
    # delay and aligned with the recipient's Project-filtered Inbox.
    payload = {
        "title": inbox_row.get("title") or inbox_row.get("project_name") or "avibe",
        "body": (message.get("text") or inbox_row.get("preview_text") or "").strip()[:240],
        "url": f"/chat/{message['session_id']}",
        "tag": f"session:{message['session_id']}",
        "message_id": message.get("id"),
        "session_id": message.get("session_id"),
    }
    thread = threading.Thread(target=_send_to_enabled_subscriptions, args=(payload,), daemon=True)
    thread.start()


def _message_still_unread(conn: Any, message_id: str | None) -> bool:
    if not message_id:
        return False
    row = conn.execute(
        select(messages.c.type, messages.c.read_at, messages.c.metadata_json)
        .where(messages.c.id == message_id)
        .where(messages.c.platform == "avibe")
        .where(messages.c.author == "agent")
        .where(messages.c.type.in_(_NOTIFIABLE_TYPES))
    ).first()
    return bool(
        row is not None
        and _is_notifiable_message(row[0], _parse_metadata(row[2]))
        and (row[0] not in _UNREAD_GATED_TYPES or row[1] is None)
    )


def _metadata_user_keys(metadata: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    user_key = metadata.get(WEB_PUSH_USER_KEY_METADATA)
    if isinstance(user_key, str) and user_key.strip():
        keys.append(user_key.strip())

    user_keys = metadata.get(WEB_PUSH_USER_KEYS_METADATA)
    if isinstance(user_keys, list):
        keys.extend(key.strip() for key in user_keys if isinstance(key, str) and key.strip())

    return list(dict.fromkeys(keys))


def web_push_authorization_context_record(
    user_key: str,
    context: AuthorizationContext | None,
) -> dict[str, Any] | None:
    """Serialize the trusted remote claims needed to recheck Project access."""

    if (
        not user_key.startswith("remote:")
        or context is None
        or not context.is_remote
        or not context.subject
        or user_key != f"remote:{context.subject}"
    ):
        return None
    record: dict[str, Any] = {
        "user_key": user_key,
        "sub": context.subject,
        "vibe_instance_role": context.instance_role,
        "vibe_instance_access_source": context.instance_access_source,
        "vibe_group_ids": sorted(context.group_ids),
    }
    optional_claims = {
        "email": context.email,
        "vibe_instance_id": context.instance_id,
        "vibe_organization_id": context.organization_id,
        "vibe_organization_member_id": context.organization_member_id,
        "vibe_organization_role": context.organization_role,
        "vibe_membership_version": context.membership_version,
    }
    record.update({key: value for key, value in optional_claims.items() if value is not None})
    return record


def _metadata_authorization_contexts(metadata: dict[str, Any]) -> dict[str, AuthorizationContext]:
    raw_contexts = metadata.get(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA)
    if not isinstance(raw_contexts, list):
        return {}
    contexts: dict[str, AuthorizationContext] = {}
    for raw_context in raw_contexts:
        if not isinstance(raw_context, dict):
            continue
        user_key = raw_context.get("user_key")
        if not isinstance(user_key, str) or not user_key.startswith("remote:"):
            continue
        context = context_from_session_payload(raw_context)
        if context.subject and user_key == f"remote:{context.subject}" and context.can_read_instance:
            contexts[user_key] = context
    return contexts


def _web_push_owner_metadata_for_message(
    conn: Any,
    message_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve trusted browser-owner metadata for a Workbench agent message.

    New sessions do not write a Web Push owner field: that made future behavior
    depend on session creation time. For upgraded rows, still honor the legacy
    stored session owner, but only after checking newer trusted user-message
    metadata.
    """

    if not message_id:
        return None, {}
    agent_row = conn.execute(
        select(
            messages.c.session_id,
            messages.c.created_at,
            messages.c.id,
            messages.c.type,
            messages.c.metadata_json,
        )
        .where(messages.c.id == message_id)
        .where(messages.c.platform == "avibe")
        .where(messages.c.author == "agent")
        .where(messages.c.type.in_(_NOTIFIABLE_TYPES))
    ).first()
    if not agent_row or not agent_row[0] or not _is_notifiable_message(
        agent_row[3],
        _parse_metadata(agent_row[4]),
    ):
        return None, {}
    session_id, created_at, row_id = agent_row[0], agent_row[1], agent_row[2]

    user_rows = conn.execute(
        select(messages.c.metadata_json)
        .where(messages.c.session_id == session_id)
        .where(messages.c.platform == "avibe")
        .where(messages.c.author == "user")
        .where(messages.c.type == "user")
        .where(messages.c.metadata_json.is_not(None))
        .where(
            or_(
                messages.c.metadata_json.contains(WEB_PUSH_USER_KEY_METADATA),
                messages.c.metadata_json.contains(WEB_PUSH_USER_KEYS_METADATA),
            )
        )
        .where(
            (messages.c.created_at < created_at)
            | ((messages.c.created_at == created_at) & (messages.c.id < row_id))
        )
        .order_by(messages.c.created_at.desc(), messages.c.id.desc())
    ).all()
    for user_row in user_rows:
        try:
            metadata = json.loads(user_row[0] or "{}") or {}
        except (TypeError, ValueError):
            continue
        if isinstance(metadata, dict) and _metadata_user_keys(metadata):
            return str(session_id), metadata

    session_metadata = conn.execute(
        select(agent_sessions.c.metadata_json).where(agent_sessions.c.id == session_id)
    ).scalar_one_or_none()
    try:
        metadata = json.loads(session_metadata or "{}") or {}
    except (TypeError, ValueError):
        return str(session_id), {}
    return str(session_id), metadata if isinstance(metadata, dict) else {}


def _web_push_user_keys_for_message(conn: Any, message_id: str | None) -> list[str]:
    """Resolve trusted browser owners for a Workbench agent message."""

    _session_id, metadata = _web_push_owner_metadata_for_message(conn, message_id)
    return _metadata_user_keys(metadata)


def _filter_project_authorized_user_keys(
    conn: Any,
    *,
    session_id: str | None,
    metadata: dict[str, Any],
    user_keys: list[str],
) -> list[str]:
    """Recheck delayed remote deliveries against the current Project policy."""

    if not session_id or not user_keys:
        return user_keys
    from storage import project_access_service

    scope_id = conn.execute(
        select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id).limit(1)
    ).scalar_one_or_none()
    project_id = project_access_service.project_id_from_scope_id(scope_id)
    if project_id is None:
        return user_keys
    policy = project_access_service.get_project_policy(conn, project_id)
    if project_access_service.is_active_project(conn, project_id) and (
        policy is None or policy.get("mode") != "restricted"
    ):
        return user_keys

    contexts = _metadata_authorization_contexts(metadata)
    authorized: list[str] = []
    for user_key in user_keys:
        if user_key == "local":
            authorized.append(user_key)
            continue
        context = contexts.get(user_key)
        if context is not None and project_access_service.role_allows(
            project_access_service.get_effective_session_role(conn, context, session_id),
            "viewer",
        ):
            authorized.append(user_key)
    return authorized


def _badge_count_for_user_key(
    conn: Any,
    *,
    user_key: str,
    contexts: dict[str, AuthorizationContext],
) -> int:
    """Return the unread count visible to one Push subscription owner."""

    if user_key == "local":
        return messages_service.total_unread(conn, platform="avibe")
    context = contexts.get(user_key)
    if context is None:
        # Legacy remote messages predate persisted authorization claims. Keep
        # delivering eligible content, but never attach a machine-global count.
        return 0
    if context.is_instance_owner:
        return messages_service.total_unread(conn, platform="avibe")

    from storage import project_access_service

    scope_ids = [
        project_access_service.project_scope_id(project_id)
        for project_id in project_access_service.accessible_project_ids(conn, context)
    ]
    return messages_service.total_unread(
        conn,
        platform="avibe",
        scope_ids=scope_ids,
    )


def _remote_access_enabled() -> bool:
    try:
        from core.services import settings as settings_service

        config = settings_service.load_config()
        cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
        return bool(cloud is not None and cloud.enabled)
    except Exception:
        logger.debug("web push: could not load remote access config", exc_info=True)
        return True


def _send_to_enabled_subscriptions(payload: dict[str, Any]) -> None:
    from core.web_push import send_web_push
    from storage.db import create_sqlite_engine

    delay = max(0.0, WEB_PUSH_NOTIFICATION_DELAY_SECONDS)
    if delay:
        time.sleep(delay)

    engine = create_sqlite_engine()
    try:
        with engine.connect() as conn:
            if not _message_still_unread(conn, payload.get("message_id")):
                logger.debug("web push: skip notification for message already read or missing")
                return
            session_id, owner_metadata = _web_push_owner_metadata_for_message(
                conn,
                payload.get("message_id"),
            )
            user_keys = _filter_project_authorized_user_keys(
                conn,
                session_id=session_id,
                metadata=owner_metadata,
                user_keys=_metadata_user_keys(owner_metadata),
            )
            if not user_keys and not _remote_access_enabled():
                user_keys = ["local"] if web_push_service.has_enabled_user_key(conn, user_key="local") else []
            if not user_keys:
                logger.debug("web push: skip notification without a unique subscription owner")
                return
            contexts = _metadata_authorization_contexts(owner_metadata)
            badge_counts = {
                user_key: _badge_count_for_user_key(
                    conn,
                    user_key=user_key,
                    contexts=contexts,
                )
                for user_key in user_keys
            }
            deliveries = []
            seen_endpoints: set[str] = set()
            for user_key in user_keys:
                for subscription in web_push_service.list_enabled(conn, user_key=user_key):
                    endpoint = subscription.get("endpoint")
                    if not isinstance(endpoint, str) or endpoint in seen_endpoints:
                        continue
                    seen_endpoints.add(endpoint)
                    deliveries.append((subscription, badge_counts[user_key]))
        for subscription, badge_count in deliveries:
            try:
                send_web_push(
                    subscription=subscription,
                    payload={**payload, "badge_count": badge_count},
                )
                with engine.begin() as conn:
                    web_push_service.mark_send_success(conn, endpoint=subscription["endpoint"])
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                disable = status_code in {404, 410}
                logger.warning("web push: send failed", exc_info=True)
                with engine.begin() as conn:
                    web_push_service.mark_send_failure(
                        conn,
                        endpoint=subscription["endpoint"],
                        disable=disable,
                    )
    finally:
        engine.dispose()
