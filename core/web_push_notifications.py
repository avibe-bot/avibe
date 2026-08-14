"""Notification dispatch from durable Workbench inbox events to Web Push."""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import and_, or_, select

from storage import messages_service, web_push_service
from storage.models import agent_sessions, messages
from vibe.message_types import spec_for, types_with
from vibe.authorization import AuthorizationContext, context_from_session_payload

logger = logging.getLogger(__name__)

_NOTIFIABLE_TYPES = {
    *types_with("webPush"),
    *types_with("webPushWhenEvents"),
}
_UNREAD_GATED_TYPES = set(types_with("unread"))
WEB_PUSH_NOTIFICATION_DELAY_SECONDS = 3.0
WEB_PUSH_USER_KEY_METADATA = "_web_push_user_key"
WEB_PUSH_USER_KEYS_METADATA = "_web_push_user_keys"
WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA = "_web_push_authorization_contexts"

# Structured authorization/delivery dispositions shared by the normal delivery
# path and the Web Push test/status diagnostics surface (#1434). A disposition
# names the gate that made one delivery decision; it never carries notification
# content or credentials.
WEB_PUSH_DISPOSITION_SENT = "sent"
WEB_PUSH_DISPOSITION_NO_OWNER = "no_owner"
WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED = "authorization_refresh_required"
WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE = "revision_unavailable"
WEB_PUSH_DISPOSITION_REVOKED = "revoked"
WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION = "no_subscription"
WEB_PUSH_DISPOSITION_PROVIDER_FAILURE = "provider_failure"
WEB_PUSH_DISPOSITION_SUPPRESSED_READ = "suppressed_read"

_ORGANIZATION_REVISION_DISPOSITIONS = {
    "unsigned": (
        WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED,
        "organization claims lack a signed authorization revision",
    ),
    "unavailable": (
        WEB_PUSH_DISPOSITION_REVISION_UNAVAILABLE,
        "organization authorization revision unavailable after one bounded retry",
    ),
    "mismatch": (
        WEB_PUSH_DISPOSITION_REVOKED,
        "signed authorization revision is no longer current",
    ),
}

_RECENT_DELIVERY_DISPOSITIONS: collections.deque[dict[str, Any]] = collections.deque(maxlen=64)
_RECENT_DELIVERY_LOCK = threading.Lock()


@dataclass(frozen=True)
class OwnerAuthorizationDecision:
    """One subscription owner's notification authorization decision."""

    user_key: str
    policy: str
    context: AuthorizationContext | None
    authorized: bool
    disposition: str | None
    reason: str


def _load_notification_config() -> Any:
    try:
        from core.services import settings as settings_service

        return settings_service.load_config()
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("web push: could not load authorization config", exc_info=True)
        return None


def _notification_policy_for_record(config: Any, record: Mapping[str, Any]) -> str:
    """Select the notification authorization policy for one persisted record.

    Policy selection follows the paired Instance kind (#1433): Personal and
    Organization notification authorization are independent policies. Legacy
    pairings with an unknown kind fall back to the record's own claim shape —
    claims issued through Organization membership follow the Organization
    policy, everything else follows the Personal policy.
    """

    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    instance_kind = str(getattr(cloud, "instance_kind", "") or "")
    if instance_kind == "organization":
        return "organization"
    if instance_kind == "personal":
        return "personal"
    organization_id = record.get("vibe_organization_id")
    if (isinstance(organization_id, str) and organization_id) or (
        record.get("vibe_instance_access_source") == "organization_group"
    ):
        return "organization"
    return "personal"


def _retry_authorization_revision_sync(config: Any) -> None:
    """Refresh the device watermark once, bounded by the device request timeout."""

    from vibe import remote_access

    try:
        remote_access.sync_authorization_revision_once(config)
    except Exception:
        logger.debug("web push: authorization revision sync retry failed", exc_info=True)


def _evaluate_record_authorization(
    config: Any,
    user_key: str,
    record: Mapping[str, Any],
    *,
    allow_sync_retry: bool = True,
) -> OwnerAuthorizationDecision:
    """Authorize one persisted prompt snapshot under its notification policy.

    Personal policy: the installed PWA's subscription is durable state, so
    delivery follows the persisted signed snapshot. It is not gated on the
    interactive authorization refresh cutoff or on Organization membership,
    group, or authorization-revision state, and ordinary Personal
    sliding-session renewal never strands the subscription (#1433/#1434).

    Organization policy: current access is resolved at delivery time through
    the instance authorization revision watermark. A confirmed change (signed
    revision no longer current) stops delivery, while temporary revision or
    control-plane unavailability gets exactly one bounded retry and is never
    treated as confirmed revocation.
    """

    from vibe import remote_access

    policy = _notification_policy_for_record(config, record)
    context = context_from_session_payload(record)
    if not (
        context.subject
        and user_key == f"remote:{context.subject}"
        and context.can_read_instance
    ):
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED,
            reason="persisted snapshot does not carry a readable instance role for this owner",
        )
    if policy == "personal":
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=context,
            authorized=True,
            disposition=None,
            reason="personal access resolved from the persisted signed snapshot",
        )
    revision_state = remote_access.session_authorization_revision_state(config, record)
    if revision_state == "unavailable" and allow_sync_retry:
        _retry_authorization_revision_sync(config)
        revision_state = remote_access.session_authorization_revision_state(config, record)
    if revision_state in {"current", "not_configured"}:
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=context,
            authorized=True,
            disposition=None,
            reason=(
                "organization access current at delivery time"
                if revision_state == "current"
                else "organization revision sync not configured"
            ),
        )
    disposition, reason = _ORGANIZATION_REVISION_DISPOSITIONS.get(
        revision_state,
        (WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED, "unknown revision state"),
    )
    return OwnerAuthorizationDecision(
        user_key=user_key,
        policy=policy,
        context=context,
        authorized=False,
        disposition=disposition,
        reason=reason,
    )


def _resolve_owner_authorization_decisions(
    metadata: dict[str, Any],
    *,
    allow_sync_retry: bool = True,
) -> dict[str, OwnerAuthorizationDecision]:
    """Resolve per-owner notification authorization from persisted records."""

    raw_contexts = metadata.get(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA)
    if not isinstance(raw_contexts, list):
        return {}
    config = _load_notification_config()
    decisions: dict[str, OwnerAuthorizationDecision] = {}
    for raw_context in raw_contexts:
        if not isinstance(raw_context, dict):
            continue
        user_key = raw_context.get("user_key")
        if not isinstance(user_key, str) or not user_key.startswith("remote:"):
            continue
        if user_key in decisions:
            continue
        decisions[user_key] = _evaluate_record_authorization(
            config,
            user_key,
            raw_context,
            allow_sync_retry=allow_sync_retry,
        )
    return decisions


def recent_delivery_dispositions(
    user_key: str | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent delivery dispositions, newest first.

    ``user_key`` scopes the report to attempts that considered that owner, so
    the test/status surface explains only the calling owner's deliveries.
    """

    with _RECENT_DELIVERY_LOCK:
        entries = list(_RECENT_DELIVERY_DISPOSITIONS)
    scoped = [
        entry
        for entry in entries
        if user_key is None or user_key in entry.get("owners", {})
    ]
    return list(reversed(scoped[-limit:]))


def evaluate_delivery_authorization_for_context(
    user_key: str,
    context: AuthorizationContext | None,
) -> dict[str, Any]:
    """Explain whether normal delivery would currently reach one owner.

    Shared by the Web Push test/status surface so a successful test send can
    be compared against the authorization gates only the normal path applies.
    Read-only: it never retries the revision sync and never delivers anything.
    """

    if user_key == "local":
        return {
            "user_key": user_key,
            "policy": "local",
            "authorized": True,
            "disposition": None,
            "reason": "local install namespace; no remote authorization gates apply",
        }
    record = web_push_authorization_context_record(user_key, context)
    if record is None:
        return {
            "user_key": user_key,
            "policy": "unknown",
            "authorized": False,
            "disposition": WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED,
            "reason": "no usable authorization snapshot for this owner",
        }
    config = _load_notification_config()
    decision = _evaluate_record_authorization(
        config,
        user_key,
        record,
        allow_sync_retry=False,
    )
    evaluation: dict[str, Any] = {
        "user_key": user_key,
        "policy": decision.policy,
        "authorized": decision.authorized,
        "disposition": decision.disposition,
        "reason": decision.reason,
    }
    if decision.policy == "organization":
        from vibe import remote_access

        evaluation["revision_state"] = remote_access.session_authorization_revision_state(
            config,
            record,
        )
    return evaluation


def _new_delivery_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message_id": payload.get("message_id"),
        "session_id": payload.get("session_id"),
        "owners": {},
        "disposition": None,
    }


def _finish_delivery_attempt(attempt: dict[str, Any], disposition: str) -> None:
    attempt["disposition"] = disposition
    with _RECENT_DELIVERY_LOCK:
        _RECENT_DELIVERY_DISPOSITIONS.append(attempt)


def _is_notifiable_message(message_type: Any, metadata: Any = None) -> bool:
    normalized_type = str(message_type or "").strip()
    spec = spec_for(normalized_type)
    return bool(
        spec["webPush"]
        or (
            isinstance(metadata, dict)
            and metadata.get("event") in spec["webPushWhenEvents"]
        )
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

    # ``session_id`` used to be a hard requirement, which made this the LAST rung of
    # the failure-notification ladder to be empty rather than the one that always
    # resolves. A definition created from a plain CLI invocation has no caller
    # provenance at all (``_session_creation_metadata_from_caller`` returns ``{}``
    # for a ``None`` caller), so it has no channel to fall back to and no user to DM;
    # an unscoped ``create_per_run`` definition can have no delivery key either. For
    # such a definition every earlier rung is empty, and a notice with nowhere to go
    # is a notice that is never written — for exactly the runs nobody is watching.
    #
    # A workspace-addressed notice needs no session because it is addressed to the
    # workspace. Where a session IS named the deep link still points at it.
    session_id = message.get("session_id")
    url = f"/chat/{session_id}" if session_id else "/harness"
    tag = f"session:{session_id}" if session_id else "harness:failure"

    # ``badge_count`` is computed per subscription owner at send time, not from
    # this one session's unread count. That keeps it current after the debounce
    # delay and aligned with the recipient's Project-filtered Inbox.
    payload = {
        "title": inbox_row.get("title") or inbox_row.get("project_name") or "avibe",
        "body": (message.get("text") or inbox_row.get("preview_text") or "").strip()[:240],
        "url": url,
        "tag": tag,
        "message_id": message.get("id"),
        "session_id": session_id,
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
        or context.claims_issued_at is None
        or user_key != f"remote:{context.subject}"
    ):
        return None
    record: dict[str, Any] = {
        "user_key": user_key,
        "sub": context.subject,
        "vibe_instance_role": context.instance_role,
        "vibe_instance_access_source": context.instance_access_source,
        "vibe_group_ids": sorted(context.group_ids),
        "claims_issued_at": context.claims_issued_at,
    }
    optional_claims = {
        "email": context.email,
        "vibe_instance_id": context.instance_id,
        "vibe_organization_id": context.organization_id,
        "vibe_organization_member_id": context.organization_member_id,
        "vibe_organization_role": context.organization_role,
        "vibe_membership_version": context.membership_version,
        "vibe_instance_authorization_revision": context.authorization_revision,
    }
    record.update({key: value for key, value in optional_claims.items() if value is not None})
    return record


def _metadata_authorization_contexts(metadata: dict[str, Any]) -> dict[str, AuthorizationContext]:
    return {
        user_key: decision.context
        for user_key, decision in _resolve_owner_authorization_decisions(metadata).items()
        if decision.authorized and decision.context is not None
    }


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
            messages_service.transcript_order_value(messages).label("transcript_at"),
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
    session_id, transcript_at, row_id = agent_row[0], agent_row[1], agent_row[2]

    user_order = messages_service.transcript_order_value(messages)

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
            or_(
                user_order < transcript_at,
                and_(user_order == transcript_at, messages.c.id < row_id),
            )
        )
        .order_by(user_order.desc(), messages.c.id.desc())
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
    user_keys: list[str],
    contexts: dict[str, AuthorizationContext],
) -> list[str]:
    """Recheck delayed remote deliveries against the current Project policy."""

    user_keys = [
        user_key
        for user_key in user_keys
        if user_key == "local" or user_key in contexts
    ]
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

    attempt = _new_delivery_attempt(payload)
    engine = create_sqlite_engine()
    try:
        with engine.connect() as conn:
            if not _message_still_unread(conn, payload.get("message_id")):
                logger.debug("web push: skip notification for message already read or missing")
                _finish_delivery_attempt(attempt, WEB_PUSH_DISPOSITION_SUPPRESSED_READ)
                return
            session_id, owner_metadata = _web_push_owner_metadata_for_message(
                conn,
                payload.get("message_id"),
            )
            decisions = _resolve_owner_authorization_decisions(owner_metadata)
            user_keys = _metadata_user_keys(owner_metadata)
            for user_key in user_keys:
                decision = decisions.get(user_key)
                if decision is None:
                    attempt["owners"][user_key] = {
                        "policy": "unknown",
                        "disposition": WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED,
                        "reason": "no persisted authorization snapshot for this owner",
                    }
                else:
                    attempt["owners"][user_key] = {
                        "policy": decision.policy,
                        "disposition": decision.disposition,
                        "reason": decision.reason,
                    }
                if decision is not None and not decision.authorized:
                    logger.warning(
                        "web push: skipping owner %s under %s policy: %s",
                        user_key,
                        decision.policy,
                        decision.reason,
                    )
            contexts = {
                user_key: decision.context
                for user_key, decision in decisions.items()
                if decision.authorized and decision.context is not None
            }
            authorized_keys = [
                user_key
                for user_key in user_keys
                if user_key == "local"
                or (decisions.get(user_key) is not None and decisions[user_key].authorized)
            ]
            project_authorized_keys = _filter_project_authorized_user_keys(
                conn,
                session_id=session_id,
                user_keys=authorized_keys,
                contexts=contexts,
            )
            for user_key in authorized_keys:
                if user_key not in project_authorized_keys:
                    attempt["owners"][user_key] = {
                        "policy": attempt["owners"].get(user_key, {}).get("policy") or "unknown",
                        "disposition": WEB_PUSH_DISPOSITION_REVOKED,
                        "reason": "current Project access policy no longer includes this owner",
                    }
                    logger.warning(
                        "web push: skipping owner %s: current Project access policy excludes it",
                        user_key,
                    )
            authorized_keys = project_authorized_keys
            if not authorized_keys and not _remote_access_enabled():
                authorized_keys = ["local"] if web_push_service.has_enabled_user_key(conn, user_key="local") else []
            if authorized_keys == ["local"] and "local" not in attempt["owners"]:
                attempt["owners"]["local"] = {
                    "policy": "local",
                    "disposition": None,
                    "reason": "local fallback with remote access disabled",
                }
            if not authorized_keys:
                disposition = next(
                    (
                        owner["disposition"]
                        for owner in attempt["owners"].values()
                        if owner.get("disposition")
                    ),
                    WEB_PUSH_DISPOSITION_NO_OWNER,
                )
                _finish_delivery_attempt(attempt, disposition)
                logger.info(
                    "web push: notification for message %s skipped: %s",
                    payload.get("message_id"),
                    disposition,
                )
                return
            badge_counts = {
                user_key: _badge_count_for_user_key(
                    conn,
                    user_key=user_key,
                    contexts=contexts,
                )
                for user_key in authorized_keys
            }
            deliveries = []
            seen_endpoints: set[str] = set()
            for user_key in authorized_keys:
                for subscription in web_push_service.list_enabled(conn, user_key=user_key):
                    endpoint = subscription.get("endpoint")
                    if not isinstance(endpoint, str) or endpoint in seen_endpoints:
                        continue
                    seen_endpoints.add(endpoint)
                    deliveries.append((subscription, badge_counts[user_key]))
        if not deliveries:
            _finish_delivery_attempt(attempt, WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION)
            logger.info(
                "web push: notification for message %s skipped: no enabled subscription",
                payload.get("message_id"),
            )
            return
        sent_count = 0
        failed_count = 0
        for subscription, badge_count in deliveries:
            try:
                send_web_push(
                    subscription=subscription,
                    payload={**payload, "badge_count": badge_count},
                )
                with engine.begin() as conn:
                    web_push_service.mark_send_success(conn, endpoint=subscription["endpoint"])
                sent_count += 1
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
                failed_count += 1
        _finish_delivery_attempt(
            attempt,
            WEB_PUSH_DISPOSITION_SENT if sent_count else WEB_PUSH_DISPOSITION_PROVIDER_FAILURE,
        )
    finally:
        engine.dispose()
