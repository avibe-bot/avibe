"""Mobile eligibility, notifications and durable value-free continuations."""

from __future__ import annotations

import json
import logging
import time

from core.mobile_health_contract import MobileError, PHONE_ERRORS, TERMINAL
from storage.mobile_store import MobileStore, mobile_store_path, publish_mobile_schedule_changed

logger = logging.getLogger(__name__)


def live_owner(config=None):
    """Fetch owner authority without opening private Health storage."""
    from config.v2_config import V2Config
    from vibe import permissions
    from vibe.binding_admission import reset_in_progress
    from storage.mobile_store import owner_unavailable

    try:
        config = config or V2Config.load()
        cloud = config.remote_access.vibe_cloud
        if reset_in_progress() or cloud.runtime_credentials() is None:
            raise owner_unavailable("cloud_unavailable")
        result = permissions.get_current_permissions(config)
    except (permissions.PermissionsError, FileNotFoundError, OSError, ValueError):
        raise owner_unavailable("cloud_unavailable") from None
    if result.source != "live":
        raise owner_unavailable("cloud_unavailable")
    instance = result.projection["instance"]
    record = result.projection["access"].get("owner")
    if not cloud.instance_id or instance["id"] != cloud.instance_id:
        raise owner_unavailable("cloud_unavailable")
    if not isinstance(record, dict):
        raise owner_unavailable("owner_record_missing")
    subject = record.get("user_id")
    if "user_id" in record and (not isinstance(subject, str) or not subject.strip() or subject != subject.strip()):
        raise owner_unavailable("owner_record_missing")
    email = (record.get("email") or "").strip().casefold()
    if subject is None and not email:
        raise owner_unavailable("cloud_owner_subject_unsupported")
    return {"id": instance["id"], "name": instance.get("name") or instance["id"],
            "owner": email, "owner_subject": subject}


def current_identity(store: MobileStore, *, retire=True) -> dict:
    """Only live owner proof can migrate or retire an enrollment."""
    from config.v2_config import V2Config, config_file_lock
    from vibe.account_session_revoke import pairing_epoch
    from vibe.binding_admission import reset_in_progress
    from vibe.remote_access import device_binding
    from storage.mobile_store import owner_unavailable

    try:
        config = V2Config.load()
    except (OSError, ValueError):
        raise owner_unavailable("cloud_unavailable") from None
    observed = store.active_device()
    identity = live_owner(config)
    # External I/O never owns local locks. Preserve both epoch and enrollment fences.
    with config_file_lock():
        current = V2Config.load()
        if (reset_in_progress() or current.remote_access.vibe_cloud.runtime_credentials() is None
                or pairing_epoch(current) != pairing_epoch(config) or device_binding(current) != device_binding(config)):
            raise owner_unavailable("cloud_unavailable")
        kwargs = dict(instance=identity["id"], owner=identity["owner"], owner_subject=identity["owner_subject"],
                      expected_device=observed["id"] if observed else None, retire=retire)
        if identity["owner_subject"] is not None:
            from storage.db import get_cached_sqlite_engine
            from storage.importer import ensure_sqlite_state
            from storage.models import device_health_links
            from sqlalchemy import select
            ensure_sqlite_state()
            with get_cached_sqlite_engine().begin() as conn:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                def proof_for(column, value):
                    # The private transaction asks only for exact live migration candidates.
                    return conn.execute(select(device_health_links).where(
                        device_health_links.c.binding == device_binding(current),
                        device_health_links.c[column] == value)).mappings().first()
                store.retire_other_identities(**kwargs, proof_for=proof_for)
        else:
            store.retire_other_identities(**kwargs)
    return identity


def context_is_owner(context, identity):
    if not context or not context.is_instance_owner or context.instance_id != identity["id"]:
        return False
    subject = identity.get("owner_subject")
    return (context.subject == subject if subject else
            bool(context.email and context.email.casefold() == identity["owner"]))


def owner_error_details(error, *, language=None):
    """Stable code, localized facts and a cause-specific next action."""
    from config.v2_config import V2Config
    from vibe.i18n import t
    reason = (error.details or {}).get("reason")
    if error.code != "instance_eligibility_unavailable" or not reason:
        return {"code": error.code, "error": error.code}
    if language is None:
        try:
            language = V2Config.load().language
        except (OSError, ValueError):
            language = "en"
    return {"code": error.code, "error": t(f"mobile.health.owner.{reason}", language),
            "details": error.details}


def request_metadata(request: dict) -> dict:
    metadata = {key: request[key] for key in (
        "id", "operation", "state", "created_at", "expires_at", "requires_user", "metrics",
    )}
    error = request.get("error")
    metadata["minimum_health_version"] = request.get("minimum_health_version", 1)
    metadata["error"] = {"code": error["code"]} if error and error.get("code") in PHONE_ERRORS else None
    return metadata


def publish_request_card(request: dict, session_id: str | None) -> None:
    if not session_id:
        return
    from core.message_mirror import mirror_harness_inbound
    from modules.im.base import MessageContext
    from storage.messages_service import NOTIFY_TYPE
    from vibe.i18n import t
    from config.v2_config import V2Config

    context = MessageContext(
        user_id="mobile", channel_id=session_id, platform="avibe",
        message_id=f"mobile:{request['id']}:created",
        platform_specific={
            "agent_session_id": session_id, "task_trigger_kind": "mobile",
            "source_kind": "mobile", "source_actor": f"mobile:{request['id']}",
            "mobile_request": request_metadata(request),
        },
    )
    if not mirror_harness_inbound(
        context, t("mobile.health.requestCreated", V2Config.load().language), message_type=NOTIFY_TYPE,
    ):
        raise MobileError("request_notification_unavailable", 503)


def notify_phone(store: MobileStore, request: dict, identity: dict, *, caller: str) -> None:
    """Only native subscriptions belonging to the current owner receive this kind."""
    from core.web_push import is_native_relay_subscription, send_web_push
    from storage import remote_access_authorization_service, web_push_service
    from storage.db import get_cached_sqlite_engine
    from vibe.authorization import context_from_session_payload
    from vibe.i18n import t
    from config.v2_config import V2Config

    language = V2Config.load().language
    with get_cached_sqlite_engine().connect() as conn:
        subscriptions = web_push_service.list_enabled(conn)
    for subscription in subscriptions:
        if not is_native_relay_subscription(subscription):
            continue
        key = subscription["user_key"]
        if not key.startswith("remote:"):
            continue
        record = remote_access_authorization_service.load_scoped(
            instance_id=identity["id"], subject=key.removeprefix("remote:"),
            scope_kind="instance", scope_ref=identity["id"],
        )
        if not record or record["authorization_state"] != "current":
            continue
        context = context_from_session_payload({**record["claims"], "sub": record["subject"], "email": record["email"]})
        if not context_is_owner(context, identity):
            continue
        # Device IDs survive subscription rotation; endpoint hashes cover older rows.
        import hashlib
        device_key = subscription.get("device_id") or hashlib.sha256(subscription["endpoint"].encode()).hexdigest()
        if not store.take_push_budget(device_key):
            continue
        action = "connect" if request["operation"] == "health.connect" else "refresh"
        payload = {
            "kind": "health.request", "request_id": request["id"], "operation": request["operation"],
            "title": t(f"mobile.health.{action}Title", language, name=caller),
            "body": t("mobile.health.notificationBody", language),
            "url": f"/mobile/health/{request['id']}", "tag": f"health:{request['id']}",
        }
        try:
            send_web_push(subscription=subscription, payload=payload)
        except Exception:
            # Provider errors may contain payloads or endpoint credentials.
            logger.warning("mobile push failed request=%s code=push_delivery_failed", request["id"])


def wait_for_request(store: MobileStore, request_id: str, seconds: float, *, waiter_id: str | None) -> dict:
    until = time.monotonic() + seconds
    try:
        while True:
            request = store.show_request(request_id)
            if request["state"] in TERMINAL:
                if waiter_id and not store.finish_wait(request_id, waiter_id, consumed=True):
                    raise MobileError("device_wait_timeout", 409, details={"request_id": request_id})
                if request["state"] == "succeeded":
                    return request
                code = request["error"]["code"] if request["error"] else (
                    "request_expired" if request["state"] == "expired" else "request_terminal"
                )
                raise MobileError(code, 409, details={"request_id": request_id, "state": request["state"]})
            if time.monotonic() >= until:
                raise MobileError("device_wait_timeout", 409, details={"request_id": request_id})
            time.sleep(min(0.1, max(0, until - time.monotonic())))
    finally:
        if waiter_id:
            store.finish_wait(request_id, waiter_id, consumed=False)


def deliver_callbacks(store: MobileStore, request_store) -> bool:
    from core.scheduled_tasks import enqueue_session_callback
    callbacks = store.pending_callbacks()
    for request in callbacks:
        # No result body or device-provided strings enter a callback.
        message = json.dumps({
            "kind": "mobile.health.result", **request_metadata(request),
            "error": request["error"],
        }, ensure_ascii=False)
        try:
            enqueue_session_callback(
                request_store, session_id=request["callback_session"],
                message=message, source_actor=f"mobile:{request['id']}",
                run_id=request["callback_run"],
            )
        except ValueError:
            # An archived or deleted callback session cannot be resumed.
            store.callback_sent(request["id"])
            continue
        store.callback_sent(request["id"])
    return bool(callbacks)


def maintenance_pass(request_store, *, check_eligibility: bool = False) -> bool:
    path = mobile_store_path()
    if not path.exists():
        publish_mobile_schedule_changed()  # Store removed outside the normal revoke flow.
        return False
    store = MobileStore(path)
    if check_eligibility:
        store.cleanup()
    if check_eligibility and store.active_device():
        try:
            current_identity(store)
        except MobileError as error:
            if error.code != "instance_eligibility_unavailable":
                logger.info("mobile policy code=%s", error.code)
    return deliver_callbacks(store, request_store)
