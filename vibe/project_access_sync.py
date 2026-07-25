"""Device-side Project index publication and access-intent convergence."""

from __future__ import annotations

from datetime import datetime
import logging
import threading
import time
from typing import Any, Mapping

import requests
from sqlalchemy import select

from config.v2_config import V2Config
from storage import project_access_service
from storage.db import create_sqlite_engine
from storage.models import project_access_policies, scope_settings, scopes

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 30
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 300
MAX_PROJECTS_PER_REQUEST = 512
_SYNC_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


class ProjectAccessSyncError(RuntimeError):
    pass


def _metadata_revision(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        return 0
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return max(0, min(int(parsed.timestamp() * 1000), project_access_service.MAX_CONTROL_PLANE_REVISION))
    except (OverflowError, ValueError):
        return 0


def project_index_descriptors() -> list[dict[str, Any]]:
    """Return the deliberately narrow Project metadata sent to the control plane."""

    engine = create_sqlite_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    scopes.c.native_id,
                    scopes.c.display_name,
                    scopes.c.updated_at,
                    scope_settings.c.enabled,
                    project_access_policies.c.last_applied_control_plane_revision,
                )
                .select_from(
                    scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id)
                    .outerjoin(
                        project_access_policies,
                        project_access_policies.c.scope_id == scopes.c.id,
                    )
                )
                .where(scopes.c.platform == "avibe", scopes.c.scope_type == "project")
                .order_by(scopes.c.native_id)
            ).all()
    finally:
        engine.dispose()
    return [
        {
            "project_id": str(row.native_id),
            "display_name": str(row.display_name or row.native_id),
            "metadata_revision": _metadata_revision(row.updated_at),
            "applied_access_revision": int(row.last_applied_control_plane_revision or 0),
            "sync_status": "deleted" if row.enabled == 0 else "in_sync",
        }
        for row in rows
    ]


def _request_json(
    method: str,
    url: str,
    *,
    device_secret: str,
    payload: Mapping[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            url,
            json=dict(payload) if payload is not None else None,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "avibe/dev",
                "X-Vibe-Device-Secret": device_secret,
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise ProjectAccessSyncError("project_access_sync_unreachable") from error
    try:
        body = response.json()
    except ValueError as error:
        raise ProjectAccessSyncError("project_access_sync_invalid_response") from error
    if not response.ok:
        code = body.get("error") if isinstance(body, dict) else None
        raise ProjectAccessSyncError(str(code or f"project_access_sync_http_{response.status_code}"))
    if not isinstance(body, dict):
        raise ProjectAccessSyncError("project_access_sync_invalid_response")
    return body


def _poll_seconds(payload: Mapping[str, Any]) -> int:
    raw = payload.get("poll_after_seconds", DEFAULT_POLL_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_POLL_SECONDS
    return max(MIN_POLL_SECONDS, min(int(raw), MAX_POLL_SECONDS))


def _acknowledge(
    endpoint: str,
    *,
    device_secret: str,
    result: project_access_service.ProjectAccessIntentResult,
) -> None:
    payload: dict[str, Any] = {
        "project_id": result.project_id,
        "revision": result.revision,
        "outcome": result.outcome,
    }
    if result.error_code:
        payload["error_code"] = result.error_code
    _request_json("POST", endpoint, device_secret=device_secret, payload=payload)


def sync_project_access_once(config: V2Config | None = None) -> dict[str, Any]:
    """Publish the local index, apply newer intents, and ACK exact revisions."""

    config = config or V2Config.load()
    cloud = config.remote_access.vibe_cloud
    if not (
        cloud.enabled
        and cloud.backend_url
        and cloud.instance_id
        and cloud.instance_secret
    ):
        return {"ok": False, "configured": False, "poll_after_seconds": DEFAULT_POLL_SECONDS}

    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "already_running", "poll_after_seconds": DEFAULT_POLL_SECONDS}
    try:
        base_url = cloud.backend_url.rstrip("/")
        base_endpoint = f"{base_url}/api/v1/instances/{cloud.instance_id}"
        descriptors = project_index_descriptors()
        poll_seconds = DEFAULT_POLL_SECONDS
        chunks = [
            descriptors[index : index + MAX_PROJECTS_PER_REQUEST]
            for index in range(0, len(descriptors), MAX_PROJECTS_PER_REQUEST)
        ] or [[]]
        for projects in chunks:
            published = _request_json(
                "PUT",
                f"{base_endpoint}/project-index",
                device_secret=cloud.instance_secret,
                payload={"projects": projects},
            )
            poll_seconds = _poll_seconds(published)

        intent_payload = _request_json(
            "GET",
            f"{base_endpoint}/project-access-intents",
            device_secret=cloud.instance_secret,
        )
        poll_seconds = _poll_seconds(intent_payload)
        raw_intents = intent_payload.get("intents")
        if not isinstance(raw_intents, list) or len(raw_intents) > MAX_PROJECTS_PER_REQUEST:
            raise ProjectAccessSyncError("project_access_sync_invalid_intents")

        engine = create_sqlite_engine()
        changed_project_ids: list[str] = []
        applied = rejected = stale = ack_errors = 0
        try:
            for intent in raw_intents:
                with engine.begin() as conn:
                    result = project_access_service.apply_project_access_intent(conn, intent)
                if result.outcome == "stale":
                    stale += 1
                    continue
                if result.outcome == "applied":
                    applied += 1
                elif result.outcome == "rejected":
                    rejected += 1
                else:
                    continue
                if result.changed:
                    changed_project_ids.append(result.project_id)
                if (
                    not result.project_id
                    or result.revision < 0
                    or result.revision > project_access_service.MAX_CONTROL_PLANE_REVISION
                ):
                    continue
                try:
                    _acknowledge(
                        f"{base_endpoint}/project-access-acks",
                        device_secret=cloud.instance_secret,
                        result=result,
                    )
                except ProjectAccessSyncError:
                    ack_errors += 1
                    logger.warning(
                        "Project access ACK failed project_id=%s revision=%s",
                        result.project_id,
                        result.revision,
                    )
        finally:
            engine.dispose()

        if changed_project_ids:
            from vibe.sse_broker import broker

            broker.publish(
                "authorization.changed",
                {"project_ids": sorted(set(changed_project_ids))},
            )
        return {
            "ok": True,
            "configured": True,
            "projects": len(descriptors),
            "intents": len(raw_intents),
            "applied": applied,
            "rejected": rejected,
            "stale": stale,
            "ack_errors": ack_errors,
            "poll_after_seconds": poll_seconds,
        }
    finally:
        _SYNC_LOCK.release()


def _worker_loop(initial_config: V2Config | None) -> None:
    config = initial_config
    delay = 0
    while True:
        if delay:
            time.sleep(delay)
        try:
            result = sync_project_access_once(config)
            delay = int(result.get("poll_after_seconds") or DEFAULT_POLL_SECONDS)
        except Exception:
            logger.warning("Project access sync failed", exc_info=True)
            delay = DEFAULT_POLL_SECONDS
        config = None


def start_project_access_sync(config: V2Config | None = None) -> None:
    global _WORKER_STARTED

    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        thread = threading.Thread(
            target=_worker_loop,
            args=(config,),
            name="vibe-project-access-sync",
            daemon=True,
        )
        _WORKER_STARTED = True
        try:
            thread.start()
        except Exception:
            _WORKER_STARTED = False
            raise
