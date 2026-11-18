"""Mobile eligibility, notifications and durable value-free continuations."""

from __future__ import annotations

import json
import logging
import time

from core.mobile_health_contract import MobileError, PHONE_ERRORS, TERMINAL
from storage.mobile_store import MobileStore

logger = logging.getLogger(__name__)


def current_identity(store: MobileStore) -> dict:
    """Use a live control-plane answer; a cached allowlist cannot prove privacy."""
    from config.v2_config import V2Config
    from vibe import permissions

    try:
        config = V2Config.load()
        cloud = config.remote_access.vibe_cloud
        if cloud.runtime_credentials() is None:
            store.revoke(caller="instance-policy")
            raise MobileError("instance_not_eligible", 403)
        result = permissions.get_current_permissions(config)
    except permissions.PermissionsError:
        raise MobileError("instance_eligibility_unavailable", 503) from None
    except (FileNotFoundError, OSError, ValueError):
        raise MobileError("instance_eligibility_unavailable", 503) from None
    if result.source != "live":
        raise MobileError("instance_eligibility_unavailable", 503)
    instance = result.projection["instance"]
    owner = result.projection["access"]["owner"]["email"]
    if (
        cloud.instance_kind != "personal"
        or instance.get("organization") is not None
        or instance["access_mode"] != "allowlist"
        or result.projection["access"]["entries"]
        or not owner or instance["id"] != cloud.instance_id
    ):
        store.revoke(caller="instance-policy")
        raise MobileError("instance_not_eligible", 403)
    identity = {"id": instance["id"], "name": instance.get("name") or instance["id"],
                "owner": owner.strip().casefold()}
    device = store.active_device()
    if device and (device["instance_id"], device["owner"]) != (identity["id"], identity["owner"]):
        store.revoke(caller="instance-policy")
    return identity


def request_metadata(request: dict) -> dict:
    metadata = {key: request[key] for key in (
        "id", "operation", "state", "created_at", "expires_at", "requires_user", "metrics",
    )}
    error = request.get("error")
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
        context = context_from_session_payload(record["claims"])
        if not (
            context.is_instance_owner and context.email
            and context.email.casefold() == identity["owner"]
            and context.instance_id == identity["id"]
        ):
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
    from config import paths
    path = paths.get_state_dir() / "mobile" / "mobile.sqlite3"
    if not path.exists():
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
