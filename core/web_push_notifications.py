"""Notification dispatch from durable Workbench inbox events to Web Push."""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import literal_column, or_, select

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
WEB_PUSH_DISPOSITION_MUTED = "muted"
WEB_PUSH_DISPOSITION_HIDDEN = "hidden"

WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE = "config_unavailable"

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
_DELIVERY_DISPOSITIONS_STATE_KEY = "web_push.recent_delivery_dispositions"
WEB_PUSH_AUTHORIZATION_SYNC_WAIT_SECONDS = 5.0

BADGE_RECONCILIATION_DELAY_SECONDS = 0.25
BADGE_RETRY_DEFAULT_SECONDS = 5.0
BADGE_RETRY_MAX_SECONDS = 60.0
BADGE_RETRY_MAX_ATTEMPTS = 3
BADGE_DISPOSITION_SENT = "badge_sent"
BADGE_DISPOSITION_SUPPRESSED_SAME = "suppressed_same"
BADGE_DISPOSITION_NO_NATIVE_SUBSCRIPTION = "no_native_subscription"
BADGE_DISPOSITION_FAILED = "failed"
_RECENT_BADGE_DISPOSITIONS: collections.deque[dict[str, Any]] = collections.deque(maxlen=64)
_RECENT_BADGE_LOCK = threading.Lock()
_BADGE_DISPOSITIONS_STATE_KEY = "web_push.recent_badge_dispositions"


@dataclass(frozen=True)
class OwnerAuthorizationDecision:
    """One subscription owner's notification authorization decision."""

    user_key: str
    policy: str
    context: AuthorizationContext | None
    authorized: bool
    disposition: str | None
    reason: str


@dataclass
class _PendingBadgeReconciliation:
    revision: int
    context: AuthorizationContext | None
    retry_attempts: int = 0
    delay_seconds: float = BADGE_RECONCILIATION_DELAY_SECONDS
    wakeup: threading.Event = field(default_factory=threading.Event)


_BADGE_RECONCILIATION_LOCK = threading.Lock()
_BADGE_RECONCILIATIONS: dict[str, _PendingBadgeReconciliation] = {}


def _load_notification_config() -> tuple[Any, bool]:
    """Return ``(config, load_failed)`` for the paired notification config.

    ``FileNotFoundError`` means the installation is genuinely unpaired and is
    not a failure; any other error means an existing configuration could not
    be read, which callers must treat as fail-closed for remote records.
    """

    try:
        from core.services import settings as settings_service

        return settings_service.load_config(), False
    except FileNotFoundError:
        return None, False
    except Exception:
        logger.debug("web push: could not load authorization config", exc_info=True)
        return None, True


def _binding_snapshot(config: Any) -> tuple[bool, str, int]:
    """One evaluation, one snapshot of (ready, kind, generation).

    Policy selection and the later gate MUST use this same snapshot. Mixing
    a reconciling-inferred Personal policy with a later-ready Organization
    binding is the class this closes.
    """

    from vibe import remote_access
    from storage import remote_access_authorization_service

    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    kind = str(getattr(cloud, "instance_kind", "") or "") if cloud is not None else ""
    try:
        generation = remote_access_authorization_service.current_instance_binding_generation(
            ensure=False
        )
    except Exception:
        generation = 0
    ready = False
    if kind in {"personal", "organization"} and config is not None:
        try:
            ready = bool(remote_access.binding_is_ready(config))
        except Exception:
            ready = False
    if not ready:
        # A known configured kind that is not ready (reconciling / pending /
        # mismatched) must not leak into policy selection as if it were live.
        # Mixing a reconciling-inferred Personal policy with a later-ready
        # Organization binding is the class this snapshot closes.
        kind = ""
    return ready, kind, int(generation)


def _notification_policy_for_record(
    config: Any,
    record: Mapping[str, Any],
    *,
    snapshot_kind: str = "",
) -> str:
    """Select the notification authorization policy for one persisted record.

    Policy selection follows the paired Instance kind (#1433): Personal and
    Organization notification authorization are independent policies. Legacy
    pairings with an unknown kind fall back to the record's own claim shape —
    claims issued through Organization membership follow the Organization
    policy, everything else follows the Personal policy.

    ``snapshot_kind`` is the kind captured once for this evaluation; do not
    re-read the live binding here.
    """

    instance_kind = snapshot_kind
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


def _paired_instance_id(config: Any) -> str | None:
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    if cloud is None or not cloud.enabled:
        return None
    instance_id = str(getattr(cloud, "instance_id", "") or "").strip()
    return instance_id or None


def _retry_authorization_revision_sync(config: Any) -> None:
    """Refresh the device watermark once, bounded by the device request timeout.

    When the 15-second poller is already mid-sync the request returns
    ``authorization_revision_sync_in_progress`` immediately; instead of
    discarding that, boundedly wait for the in-flight refresh to land so a
    watermark that is about to refresh does not fail this delivery.
    """

    from vibe import remote_access

    result: Any = None
    try:
        result = remote_access.sync_authorization_revision_once(config)
    except Exception:
        logger.debug("web push: authorization revision sync retry failed", exc_info=True)
        return
    if not isinstance(result, dict) or (
        result.get("error") != "authorization_revision_sync_in_progress"
    ):
        return
    deadline = time.monotonic() + WEB_PUSH_AUTHORIZATION_SYNC_WAIT_SECONDS
    while time.monotonic() < deadline:
        if config is None:
            config, _config_load_failed = _load_notification_config()
        if config is not None:
            try:
                if remote_access.current_authorization_revision(config) is not None:
                    return
            except Exception:
                logger.debug("web push: in-flight sync wait check failed", exc_info=True)
                return
        time.sleep(0.25)


def _evaluate_record_authorization(
    config: Any,
    user_key: str,
    record: Mapping[str, Any],
    *,
    config_load_failed: bool = False,
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
    from storage import remote_access_authorization_service

    snapshot_ready, snapshot_kind, snapshot_generation = _binding_snapshot(config)
    policy = _notification_policy_for_record(
        config, record, snapshot_kind=snapshot_kind
    )
    if config is None:
        reason = (
            "paired configuration could not be read; instance binding cannot be validated"
            if config_load_failed
            else "paired configuration is unavailable; instance binding cannot be validated"
        )
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason=reason,
        )
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    if cloud is not None and not cloud.enabled:
        # Disabling remote access is a confirmed, deliberate removal of every
        # remote principal's reach: no remote record may keep authorizing
        # delivery while the connector is off, regardless of pairing state.
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_REVOKED,
            reason="remote access is disabled on this installation",
        )
    if config_load_failed:
        # The paired configuration exists but could not be read: the record's
        # instance binding cannot be validated for any remote owner, so fail
        # closed for this delivery instead of guessing. The next delivery
        # retries the read.
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason="paired configuration could not be read; instance binding cannot be validated",
        )
    from vibe.authorization import instance_kind_is_unsupported

    record_kind = record.get("vibe_instance_kind")
    if instance_kind_is_unsupported(record_kind):
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_REVOKED,
            reason="persisted snapshot instance kind is unrecognized",
        )
    paired_kind = snapshot_kind
    try:
        live_generation = remote_access_authorization_service.current_instance_binding_generation(
            ensure=False
        )
    except Exception:
        live_generation = snapshot_generation
    configured_kind = str(getattr(cloud, "instance_kind", "") or "") if cloud is not None else ""
    generation_moved = int(live_generation) != int(snapshot_generation)
    if configured_kind in {"personal", "organization"} and not snapshot_ready:
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason="durable instance binding is not ready",
        )
    if (
        configured_kind in {"personal", "organization"}
        and snapshot_ready
        and generation_moved
        and int(snapshot_generation) > 0
    ):
        # The binding advanced under us after we captured a real (non-zero)
        # generation. Mixing the original policy with the new binding is the
        # class this snapshot closes. Generation 0 → N is bootstrap, not TOCTOU.
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason="durable instance binding is not ready",
        )
    if (
        paired_kind in {"personal", "organization"}
        and cloud is not None
        and cloud.runtime_credentials() is None
    ):
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason="current pairing lacks complete runtime credentials",
        )
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
    paired_instance_id = _paired_instance_id(config)
    record_instance_id = record.get("vibe_instance_id")
    if (
        paired_instance_id
        and isinstance(record_instance_id, str)
        and record_instance_id
        and record_instance_id != paired_instance_id
    ):
        # Re-pairing this installation to another instance does not clear
        # subscriptions or old prompt snapshots; a snapshot minted by the
        # previous instance must never authorize delivery on this one, under
        # either policy.
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_REVOKED,
            reason="persisted snapshot was issued for a different paired instance",
        )
    record_kind = record.get("vibe_instance_kind")
    if (
        record_kind in {"personal", "organization"}
        and paired_kind not in {"personal", "organization"}
    ):
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_CONFIG_UNAVAILABLE,
            reason="current pairing instance kind is unavailable",
        )
    if (
        record_kind in {"personal", "organization"}
        and paired_kind in {"personal", "organization"}
        and record_kind != paired_kind
    ):
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=None,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_REVOKED,
            reason="persisted snapshot instance kind differs from the current pairing",
        )
    if (
        context.instance_kind is None
        and record_kind is None
        and paired_instance_id
        and paired_kind in {"personal", "organization"}
    ):
        if record_instance_id != paired_instance_id:
            return OwnerAuthorizationDecision(
                user_key=user_key,
                policy=policy,
                context=None,
                authorized=False,
                disposition=WEB_PUSH_DISPOSITION_REVOKED,
                reason="persisted snapshot lacks a binding to the current paired instance",
            )
        context = replace(context, instance_kind=paired_kind)
    if policy == "personal":
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=context,
            authorized=True,
            disposition=None,
            reason="personal access resolved from the persisted signed snapshot",
        )
    if not _record_carries_signed_revision(record):
        # Organization delivery-time verification is only possible against a
        # signed revision; unsigned claims can never be confirmed current,
        # whether or not revision sync is configured right now.
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=context,
            authorized=False,
            disposition=WEB_PUSH_DISPOSITION_AUTHORIZATION_REFRESH_REQUIRED,
            reason="organization claims lack a signed authorization revision",
        )
    revision_state = remote_access.session_authorization_revision_state(config, record)
    if revision_state == "not_configured":
        # A revision-signed Organization snapshot proves the instance was
        # revision-synced when the snapshot was minted. Being unable to check
        # the watermark now — missing, unreadable, or unpaired local config —
        # is unavailability, not evidence that no sync ever applied, and must
        # not authorize delivery of protected output.
        revision_state = "unavailable"
    if revision_state == "unavailable" and allow_sync_retry:
        _retry_authorization_revision_sync(config)
        revision_state = remote_access.session_authorization_revision_state(config, record)
        if revision_state == "not_configured":
            revision_state = "unavailable"
    if revision_state == "current":
        return OwnerAuthorizationDecision(
            user_key=user_key,
            policy=policy,
            context=context,
            authorized=True,
            disposition=None,
            reason="organization access current at delivery time",
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


def _record_carries_signed_revision(record: Mapping[str, Any]) -> bool:
    value = record.get("vibe_instance_authorization_revision")
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _resolve_owner_authorization_decisions(
    metadata: dict[str, Any],
    *,
    allow_sync_retry: bool = True,
) -> dict[str, OwnerAuthorizationDecision]:
    """Resolve per-owner notification authorization from persisted records.

    The bounded watermark refresh is shared by the whole owner set: one merged
    prompt with several Organization owners performs at most one synchronous
    sync request per delivery, never one request timeout per owner.
    """

    raw_contexts = metadata.get(WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA)
    if not isinstance(raw_contexts, list):
        return {}
    records: list[tuple[str, dict[str, Any]]] = []
    seen_user_keys: set[str] = set()
    for raw_context in raw_contexts:
        if not isinstance(raw_context, dict):
            continue
        user_key = raw_context.get("user_key")
        if not isinstance(user_key, str) or not user_key.startswith("remote:"):
            continue
        if user_key in seen_user_keys:
            continue
        seen_user_keys.add(user_key)
        records.append((user_key, raw_context))
    if not records:
        return {}
    config, config_load_failed = _load_notification_config()
    snapshot_ready, snapshot_kind, _snapshot_generation = (
        _binding_snapshot(config) if config is not None else (False, "", 0)
    )
    if (
        allow_sync_retry
        and not config_load_failed
        and any(
            _notification_policy_for_record(
                config, record, snapshot_kind=snapshot_kind
            )
            == "organization"
            for _user_key, record in records
        )
    ):
        from vibe import remote_access

        if config is None or remote_access.current_authorization_revision(config) is None:
            _retry_authorization_revision_sync(config)
            config, config_load_failed = _load_notification_config()
    return {
        user_key: _evaluate_record_authorization(
            config,
            user_key,
            record,
            config_load_failed=config_load_failed,
            allow_sync_retry=False,
        )
        for user_key, record in records
    }


def _stored_delivery_dispositions() -> list[dict[str, Any]] | None:
    """Read the durable disposition ring from ``state_meta``.

    Normal delivery runs in the controller process while the Web Push
    test/status surface runs in the UI process, so the ring is persisted in
    SQLite rather than kept only in memory. Returns ``None`` when storage is
    unavailable so callers can fall back to the process-local deque.
    """

    try:
        from core.chat_discovery import get_state_meta

        stored = get_state_meta(_DELIVERY_DISPOSITIONS_STATE_KEY)
    except Exception:
        logger.debug("web push: could not read stored delivery dispositions", exc_info=True)
        return None
    return stored if isinstance(stored, list) else []


def _store_delivery_dispositions(entries: list[dict[str, Any]]) -> None:
    try:
        from core.chat_discovery import set_state_meta

        set_state_meta(_DELIVERY_DISPOSITIONS_STATE_KEY, entries[-_RECENT_DELIVERY_DISPOSITIONS.maxlen :])
    except Exception:
        logger.debug("web push: could not store delivery dispositions", exc_info=True)


def recent_delivery_dispositions(
    user_key: str | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent delivery dispositions, newest first.

    ``user_key`` scopes the report to attempts that considered that owner, so
    the test/status surface explains only the calling owner's deliveries.
    Reads the durable ``state_meta`` ring so deliveries recorded by the
    controller process are visible to the UI-process status surface.
    """

    stored = _stored_delivery_dispositions()
    if stored is None:
        with _RECENT_DELIVERY_LOCK:
            stored = list(_RECENT_DELIVERY_DISPOSITIONS)
    scoped: list[dict[str, Any]] = []
    for entry in stored:
        owners = entry.get("owners", {})
        if user_key is None:
            scoped.append(entry)
        elif user_key in owners:
            # One owner's diagnostics must not disclose the other merged
            # recipients or their authorization reasons.
            scoped.append({**entry, "owners": {user_key: owners[user_key]}})
    return list(reversed(scoped[-limit:]))


def recent_badge_dispositions(*, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent badge reconciliation outcomes without owner or endpoint data."""

    try:
        from core.chat_discovery import get_state_meta

        stored = get_state_meta(_BADGE_DISPOSITIONS_STATE_KEY)
    except Exception:
        logger.debug("web push badge: could not read stored dispositions", exc_info=True)
        stored = None
    if not isinstance(stored, list):
        with _RECENT_BADGE_LOCK:
            stored = list(_RECENT_BADGE_DISPOSITIONS)
    return list(reversed(stored[-limit:]))


def _record_badge_disposition(
    *,
    user_key: str,
    badge: int | None,
    disposition: str,
    native_subscriptions: int,
    sent: int = 0,
    suppressed: int = 0,
    failed: int = 0,
    reason: str | None = None,
) -> None:
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owner_ref": hashlib.sha256(user_key.encode("utf-8")).hexdigest()[:12],
        "owner_kind": "remote" if user_key.startswith("remote:") else "local",
        "badge": badge,
        "disposition": disposition,
        "native_subscriptions": native_subscriptions,
        "sent": sent,
        "suppressed": suppressed,
        "failed": failed,
    }
    if reason:
        entry["reason"] = reason
    with _RECENT_BADGE_LOCK:
        if not _RECENT_BADGE_DISPOSITIONS:
            try:
                from core.chat_discovery import get_state_meta

                stored = get_state_meta(_BADGE_DISPOSITIONS_STATE_KEY)
            except Exception:
                stored = None
            if isinstance(stored, list):
                _RECENT_BADGE_DISPOSITIONS.extend(
                    stored[-_RECENT_BADGE_DISPOSITIONS.maxlen :]
                )
        _RECENT_BADGE_DISPOSITIONS.append(entry)
        entries = list(_RECENT_BADGE_DISPOSITIONS)
        try:
            from core.chat_discovery import set_state_meta

            set_state_meta(_BADGE_DISPOSITIONS_STATE_KEY, entries)
        except Exception:
            logger.debug("web push badge: could not store disposition", exc_info=True)
    logger.info(
        "web push badge: disposition=%s badge=%s native=%s sent=%s suppressed=%s failed=%s",
        disposition,
        badge,
        native_subscriptions,
        sent,
        suppressed,
        failed,
    )


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
    config, config_load_failed = _load_notification_config()
    decision = _evaluate_record_authorization(
        config,
        user_key,
        record,
        config_load_failed=config_load_failed,
        allow_sync_retry=False,
    )
    evaluation: dict[str, Any] = {
        "user_key": user_key,
        "policy": decision.policy,
        "authorized": decision.authorized,
        "disposition": decision.disposition,
        "reason": decision.reason,
    }
    if decision.policy == "organization" and config is not None:
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
    # The deque snapshot and the durable `state_meta` write stay inside one
    # lock-held critical section: two concurrent delivery threads could
    # otherwise let the older snapshot overwrite the newer persisted entry.
    # The deque is hydrated from the persisted ring first so a process restart
    # (empty deque, stored history intact) appends instead of truncating.
    with _RECENT_DELIVERY_LOCK:
        if not _RECENT_DELIVERY_DISPOSITIONS:
            stored = _stored_delivery_dispositions()
            if stored:
                _RECENT_DELIVERY_DISPOSITIONS.extend(
                    stored[-_RECENT_DELIVERY_DISPOSITIONS.maxlen :]
                )
        _RECENT_DELIVERY_DISPOSITIONS.append(attempt)
        entries = list(_RECENT_DELIVERY_DISPOSITIONS)
        _store_delivery_dispositions(entries)


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
    url = _notification_url(session_id, message.get("id"))
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


def _notification_url(session_id: Any, message_id: Any) -> str:
    """The page a tap opens: the chat at this message, marked as a notification.

    ``msg`` is ChatPage's deep-link anchor. ``from=notification`` sends a tap
    whose chat is gone or no longer readable to the Inbox instead of an error.
    """

    if not session_id:
        return "/harness"
    query = {"msg": message_id} if message_id else {}
    query["from"] = "notification"
    return f"/chat/{urllib.parse.quote(str(session_id), safe='')}?{urllib.parse.urlencode(query)}"


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
        "vibe_instance_kind": context.instance_kind,
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

    Messages enter this ledger on acceptance, not submission. SQLite's rowid
    orders those inserts even when the wall clock steps backwards; timestamp-
    prefixed message IDs and transcript timestamps do not. Keep the result's
    rowid as an upper bound so a later prompt cannot steal its notification.
    """

    if not message_id:
        return None, {}
    insertion_order = literal_column("messages.rowid")
    agent_row = conn.execute(
        select(
            messages.c.session_id,
            insertion_order,
            messages.c.type,
            messages.c.metadata_json,
        )
        .where(messages.c.id == message_id)
        .where(messages.c.platform == "avibe")
        .where(messages.c.author == "agent")
        .where(messages.c.type.in_(_NOTIFIABLE_TYPES))
    ).first()
    if not agent_row or not agent_row[0] or not _is_notifiable_message(
        agent_row[2],
        _parse_metadata(agent_row[3]),
    ):
        return None, {}
    session_id, result_insertion_order = agent_row[0], agent_row[1]

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
        .where(insertion_order < result_insertion_order)
        .order_by(insertion_order.desc())
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


def _owners_hidden_from_session(
    conn: Any,
    *,
    session_id: str | None,
    user_keys: list[str],
    contexts: dict[str, AuthorizationContext],
) -> list[str]:
    """Owners whose Inbox leaves this Session out, so a push would open a conversation they cannot see.

    ``hidden_bot_session_ids`` owns that set, as it does for ``GET /api/inbox`` and the
    event stream: a removed Bot's Session for the Owner, every Bot's for anyone else
    (``docs/plans/bots.md`` D1). Only ids in the Bots' namespace pay for the read. A
    remote owner without a current context is treated as unable to see it.
    """

    from storage.agent_session_rows import is_bot_session_id

    if not session_id or not is_bot_session_id(session_id):
        return []
    from core.bots import hidden_bot_session_ids

    hidden: list[str] = []
    for user_key in user_keys:
        context = None if user_key == "local" else contexts.get(user_key)
        if user_key != "local" and context is None:
            hidden.append(user_key)
        elif session_id in hidden_bot_session_ids(conn, context):
            hidden.append(user_key)
    return hidden


def _badge_count_for_user_key(
    conn: Any,
    *,
    user_key: str,
    contexts: dict[str, AuthorizationContext],
) -> int:
    """Return the loud unread count visible to one Push subscription owner."""

    from core.notification_categories import current_bot_session_ids, split_unread_counts
    from core.notification_preferences import badge_total, get_notification_preferences
    from core.bots import hidden_bot_session_ids

    hidden_session_ids = hidden_bot_session_ids(
        conn,
        None if user_key == "local" else contexts.get(user_key),
    )
    if user_key == "local":
        scope_ids = None
    else:
        context = contexts.get(user_key)
        if context is None:
            # Legacy remote messages predate persisted authorization claims. Keep
            # delivering eligible content, but never attach a machine-global count.
            return 0
        if context.can_manage_instance:
            scope_ids = None
        else:
            from storage import project_access_service

            scope_ids = [
                project_access_service.project_scope_id(project_id)
                for project_id in project_access_service.accessible_project_ids(conn, context)
            ]

    per_session = messages_service.unread_counts_by_session(
        conn,
        platform="avibe",
        scope_ids=scope_ids,
        hidden_session_ids=hidden_session_ids,
    )
    category_counts = split_unread_counts(
        per_session,
        bot_session_ids=current_bot_session_ids(conn),
    )
    return badge_total(category_counts, get_notification_preferences(user_key))


def _owner_delivery_lock(user_key: str):
    from config import paths
    from storage.lock import MigrationFileLock

    owner_hash = hashlib.sha256(user_key.encode("utf-8")).hexdigest()
    return MigrationFileLock(
        paths.get_state_dir()
        / "web_push"
        / "owner_delivery_locks"
        / f"{owner_hash}.lock",
        timeout_seconds=None,
    )


def _badge_retry_delay(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code not in {429, 503}:
        return None
    raw_retry_after = getattr(response, "headers", {}).get("retry-after")
    try:
        retry_after = float(raw_retry_after)
    except (TypeError, ValueError):
        retry_after = BADGE_RETRY_DEFAULT_SECONDS
    return min(
        BADGE_RETRY_MAX_SECONDS,
        max(BADGE_RECONCILIATION_DELAY_SECONDS, retry_after),
    )


def schedule_badge_reconciliation(
    user_key: str,
    context: AuthorizationContext | None,
) -> None:
    """Coalesce owner badge changes and reconcile them off the request path."""

    user_key = user_key.strip() if isinstance(user_key, str) else ""
    if not user_key:
        return
    start_worker = False
    with _BADGE_RECONCILIATION_LOCK:
        pending = _BADGE_RECONCILIATIONS.get(user_key)
        if pending is None:
            _BADGE_RECONCILIATIONS[user_key] = _PendingBadgeReconciliation(
                revision=1,
                context=context,
            )
            start_worker = True
        else:
            pending.revision += 1
            pending.context = context
            pending.retry_attempts = 0
            pending.delay_seconds = BADGE_RECONCILIATION_DELAY_SECONDS
            pending.wakeup.set()
    if not start_worker:
        return
    worker = threading.Thread(
        target=_badge_reconciliation_worker,
        args=(user_key,),
        daemon=True,
    )
    try:
        worker.start()
    except Exception:
        with _BADGE_RECONCILIATION_LOCK:
            _BADGE_RECONCILIATIONS.pop(user_key, None)
        logger.warning("web push badge: could not start reconciliation worker")


def _badge_reconciliation_worker(user_key: str) -> None:
    while True:
        with _BADGE_RECONCILIATION_LOCK:
            pending = _BADGE_RECONCILIATIONS.get(user_key)
            if pending is None:
                return
            revision = pending.revision
            delay = max(0.0, pending.delay_seconds)
            wakeup = pending.wakeup
            wakeup.clear()
        if delay and wakeup.wait(delay):
            continue
        with _BADGE_RECONCILIATION_LOCK:
            pending = _BADGE_RECONCILIATIONS.get(user_key)
            if pending is None:
                return
            if pending.revision != revision:
                continue
            context = pending.context
        retry_after = None
        try:
            retry_after = _reconcile_badge_for_owner(user_key, context)
        except Exception:
            logger.exception("web push badge: owner reconciliation failed before delivery")
            _record_badge_disposition(
                user_key=user_key,
                badge=None,
                disposition=BADGE_DISPOSITION_FAILED,
                native_subscriptions=0,
                failed=1,
                reason="reconciliation_error",
            )
        with _BADGE_RECONCILIATION_LOCK:
            current = _BADGE_RECONCILIATIONS.get(user_key)
            if current is None:
                return
            if current.revision != revision:
                continue
            if (
                retry_after is not None
                and current.retry_attempts < BADGE_RETRY_MAX_ATTEMPTS
            ):
                current.retry_attempts += 1
                current.delay_seconds = retry_after
                continue
            del _BADGE_RECONCILIATIONS[user_key]
            return


def _reconcile_badge_for_owner(
    user_key: str,
    context: AuthorizationContext | None,
) -> float | None:
    from core.web_push import is_native_relay_subscription, send_badge_update
    from storage.db import create_sqlite_engine

    if user_key != "local":
        authorization = evaluate_delivery_authorization_for_context(user_key, context)
        if not authorization.get("authorized"):
            _record_badge_disposition(
                user_key=user_key,
                badge=None,
                disposition=BADGE_DISPOSITION_FAILED,
                native_subscriptions=0,
                failed=1,
                reason=str(authorization.get("disposition") or "authorization_unavailable"),
            )
            return
    contexts = (
        {user_key: context}
        if user_key != "local" and context is not None
        else {}
    )
    retry_after: float | None = None
    with _owner_delivery_lock(user_key):
        engine = create_sqlite_engine()
        try:
            with engine.connect() as conn:
                badge_count = _badge_count_for_user_key(
                    conn,
                    user_key=user_key,
                    contexts=contexts,
                )
                native_subscriptions = [
                    subscription
                    for subscription in web_push_service.list_enabled(
                        conn,
                        user_key=user_key,
                    )
                    if is_native_relay_subscription(subscription)
                ]
                deliveries = [
                    subscription
                    for subscription in native_subscriptions
                    if web_push_service.get_last_successful_badge(
                        conn,
                        endpoint=subscription["endpoint"],
                    )
                    != badge_count
                ]
            if not native_subscriptions:
                _record_badge_disposition(
                    user_key=user_key,
                    badge=badge_count,
                    disposition=BADGE_DISPOSITION_NO_NATIVE_SUBSCRIPTION,
                    native_subscriptions=0,
                )
                return None
            suppressed_count = len(native_subscriptions) - len(deliveries)
            if not deliveries:
                _record_badge_disposition(
                    user_key=user_key,
                    badge=badge_count,
                    disposition=BADGE_DISPOSITION_SUPPRESSED_SAME,
                    native_subscriptions=len(native_subscriptions),
                    suppressed=suppressed_count,
                )
                return None

            sent_count = 0
            failed_count = 0
            for subscription in deliveries:
                try:
                    send_badge_update(
                        subscription=subscription,
                        badge=badge_count,
                    )
                    with engine.begin() as conn:
                        web_push_service.mark_send_success(
                            conn,
                            endpoint=subscription["endpoint"],
                            badge_count=badge_count,
                        )
                    sent_count += 1
                except Exception as exc:
                    status_code = getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )
                    with engine.begin() as conn:
                        web_push_service.mark_send_failure(
                            conn,
                            endpoint=subscription["endpoint"],
                            disable=status_code in {404, 410},
                        )
                    candidate_retry = _badge_retry_delay(exc)
                    if candidate_retry is not None:
                        retry_after = max(retry_after or 0.0, candidate_retry)
                    failed_count += 1
                    logger.warning(
                        "web push badge: relay request failed status=%s",
                        status_code,
                    )
            disposition = (
                BADGE_DISPOSITION_FAILED
                if failed_count
                else BADGE_DISPOSITION_SENT
            )
            _record_badge_disposition(
                user_key=user_key,
                badge=badge_count,
                disposition=disposition,
                native_subscriptions=len(native_subscriptions),
                sent=sent_count,
                suppressed=suppressed_count,
                failed=failed_count,
            )
        finally:
            engine.dispose()
    return retry_after


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
            session_id, owner_metadata = _web_push_owner_metadata_for_message(
                conn,
                payload.get("message_id"),
            )

            def _finish_suppressed_read() -> None:
                # Attribute the suppression to the resolved owners so the
                # scoped status surface can still explain this outcome.
                logger.debug("web push: skip notification for message already read or missing")
                for user_key in _metadata_user_keys(owner_metadata):
                    attempt["owners"][user_key] = {
                        "policy": "local" if user_key == "local" else "unknown",
                        "disposition": WEB_PUSH_DISPOSITION_SUPPRESSED_READ,
                        "reason": "message was read or missing before the delivery delay elapsed",
                    }
                _finish_delivery_attempt(attempt, WEB_PUSH_DISPOSITION_SUPPRESSED_READ)

            if not _message_still_unread(conn, payload.get("message_id")):
                _finish_suppressed_read()
                return
            decisions = _resolve_owner_authorization_decisions(owner_metadata)
            if not _message_still_unread(conn, payload.get("message_id")):
                # The authorization resolution may block for a bounded sync
                # retry; a message opened meanwhile must not still notify.
                _finish_suppressed_read()
                return
            user_keys = _metadata_user_keys(owner_metadata)
            for user_key in user_keys:
                decision = decisions.get(user_key)
                if user_key == "local":
                    attempt["owners"][user_key] = {
                        "policy": "local",
                        "disposition": None,
                        "reason": "local install namespace; no remote authorization gates apply",
                    }
                    continue
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

            # A Session hidden from an owner is not a muted category for them: it is not
            # in their Inbox at all, so nothing about it may reach their devices.
            hidden_keys = _owners_hidden_from_session(
                conn,
                session_id=session_id,
                user_keys=authorized_keys,
                contexts=contexts,
            )
            for user_key in hidden_keys:
                attempt["owners"][user_key] = {
                    "policy": attempt["owners"].get(user_key, {}).get("policy") or (
                        "local" if user_key == "local" else "unknown"
                    ),
                    "disposition": WEB_PUSH_DISPOSITION_HIDDEN,
                    "reason": "the Session is hidden from this owner's Inbox",
                }
                logger.info("web push: skipping owner %s: the Session is hidden from them", user_key)
            authorized_keys = [user_key for user_key in authorized_keys if user_key not in hidden_keys]

            from core.notification_categories import category_for_session_id, current_bot_session_ids
            from core.notification_preferences import category_is_loud, get_notification_preferences

            notification_category = category_for_session_id(
                session_id,
                bot_session_ids=current_bot_session_ids(conn),
            )
            payload.setdefault("category", notification_category)
            loud_authorized_keys: list[str] = []
            for user_key in authorized_keys:
                preferences = get_notification_preferences(user_key)
                if category_is_loud(preferences, notification_category):
                    loud_authorized_keys.append(user_key)
                    continue
                attempt["owners"][user_key] = {
                    "policy": attempt["owners"].get(user_key, {}).get("policy") or (
                        "local" if user_key == "local" else "unknown"
                    ),
                    "disposition": WEB_PUSH_DISPOSITION_MUTED,
                    "reason": f"{notification_category} notifications are muted for this account",
                }
                logger.info(
                    "web push: skipping owner %s: %s notifications are muted",
                    user_key,
                    notification_category,
                )
            authorized_keys = loud_authorized_keys
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
            deliveries = []
            seen_endpoints: set[str] = set()
            for user_key in authorized_keys:
                for subscription in web_push_service.list_enabled(conn, user_key=user_key):
                    endpoint = subscription.get("endpoint")
                    if not isinstance(endpoint, str) or endpoint in seen_endpoints:
                        continue
                    seen_endpoints.add(endpoint)
                    deliveries.append((subscription, user_key))
        if not deliveries:
            _mark_owners_without_delivery(attempt, [])
            _finish_delivery_attempt(attempt, WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION)
            logger.info(
                "web push: notification for message %s skipped: no enabled subscription",
                payload.get("message_id"),
            )
            return
        sent_count = 0
        failed_count = 0
        suppressed_read_count = 0
        owner_sent_counts: dict[str, int] = {}
        owner_failed_counts: dict[str, int] = {}
        owner_suppressed_read_counts: dict[str, int] = {}
        for subscription, delivery_owner in deliveries:
            with _owner_delivery_lock(delivery_owner):
                with engine.connect() as conn:
                    if not _message_still_unread(conn, payload.get("message_id")):
                        suppressed_read_count += 1
                        owner_suppressed_read_counts[delivery_owner] = (
                            owner_suppressed_read_counts.get(delivery_owner, 0) + 1
                        )
                        continue
                    badge_count = _badge_count_for_user_key(
                        conn,
                        user_key=delivery_owner,
                        contexts=contexts,
                    )
                try:
                    send_web_push(
                        subscription=subscription,
                        payload={**payload, "badge_count": badge_count},
                    )
                    with engine.begin() as conn:
                        web_push_service.mark_send_success(
                            conn,
                            endpoint=subscription["endpoint"],
                            badge_count=badge_count,
                        )
                    sent_count += 1
                    owner_sent_counts[delivery_owner] = (
                        owner_sent_counts.get(delivery_owner, 0) + 1
                    )
                except Exception as exc:
                    status_code = getattr(
                        getattr(exc, "response", None),
                        "status_code",
                        None,
                    )
                    disable = status_code in {404, 410}
                    logger.warning("web push: send failed", exc_info=True)
                    with engine.begin() as conn:
                        web_push_service.mark_send_failure(
                            conn,
                            endpoint=subscription["endpoint"],
                            disable=disable,
                        )
                    failed_count += 1
                    owner_failed_counts[delivery_owner] = (
                        owner_failed_counts.get(delivery_owner, 0) + 1
                    )
        # Provider outcomes are per owner: in a merged delivery the owner whose
        # endpoint failed must not be told the attempt was sent, and an
        # authorized owner with no enabled endpoint is told so explicitly.
        _mark_owners_without_delivery(attempt, [owner for _s, owner in deliveries])
        for delivery_owner in {owner for _s, owner in deliveries}:
            owner_entry = attempt["owners"].setdefault(
                delivery_owner,
                {"policy": "unknown", "disposition": None, "reason": ""},
            )
            if owner_sent_counts.get(delivery_owner):
                owner_entry["disposition"] = WEB_PUSH_DISPOSITION_SENT
            elif owner_failed_counts.get(delivery_owner):
                owner_entry["disposition"] = WEB_PUSH_DISPOSITION_PROVIDER_FAILURE
            elif owner_suppressed_read_counts.get(delivery_owner):
                owner_entry["disposition"] = WEB_PUSH_DISPOSITION_SUPPRESSED_READ
        if sent_count:
            disposition = WEB_PUSH_DISPOSITION_SENT
        elif failed_count:
            disposition = WEB_PUSH_DISPOSITION_PROVIDER_FAILURE
        elif suppressed_read_count:
            disposition = WEB_PUSH_DISPOSITION_SUPPRESSED_READ
        else:
            disposition = WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION
        _finish_delivery_attempt(attempt, disposition)
    finally:
        engine.dispose()


def _mark_owners_without_delivery(attempt: dict[str, Any], delivered_owners: list[str]) -> None:
    """Label authorized owners that received no endpoint delivery."""

    delivered = set(delivered_owners)
    for user_key, owner_entry in attempt["owners"].items():
        if user_key not in delivered and owner_entry.get("disposition") is None:
            owner_entry["disposition"] = WEB_PUSH_DISPOSITION_NO_SUBSCRIPTION
