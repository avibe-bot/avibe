"""Controller-owned Live Activity lifecycle and bounded relay publisher."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from email.utils import parsedate_to_datetime
import json
import logging
import time
from typing import Any, Callable

from sqlalchemy.engine import Engine

from sqlalchemy import select

from core.web_push import send_live_activity_update
from storage import live_activity_service as store, web_push_service
from storage.db import create_sqlite_engine
from storage.models import live_activity_trackings as tracks

logger = logging.getLogger(__name__)


class LiveActivityService:
    def __init__(
        self,
        *,
        engine: Engine | None = None,
        clock: Callable[[], float] = time.time,
        sender: Callable[..., None] = send_live_activity_update,
        pending_question: Callable[[str], bool] | None = None,
    ) -> None:
        self.engine = engine or create_sqlite_engine()
        self._owns_engine = engine is None
        self.clock = clock
        self._started_at = int(clock())
        self.sender = sender
        self.pending_question = pending_question or (lambda _sid: False)
        self._lock = asyncio.Lock()
        self._task = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="live-activity-publisher")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._owns_engine:
            self.engine.dispose()

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Live Activity publisher sweep failed")
            await asyncio.sleep(5)

    async def handle(
        self, operation: str, owner: str, activity_id: str, payload: dict, *, authorization: dict | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            sid = payload.get("session_id")
            if not sid and activity_id:
                row = await asyncio.to_thread(self._read, activity_id)
                sid = row["session_id"] if row else None
            pending = bool(sid and self.pending_question(sid))
            return await self._joined_worker(
                self._handle, operation, owner, activity_id, payload, pending, authorization
            )

    def _read(self, activity_id):
        with self.engine.connect() as conn:
            return store.get(conn, activity_id)

    def _handle(self, operation, owner, activity_id, payload, pending, authorization):
        with self.engine.begin() as conn:
            if operation == 'put':
                # Rotation may revive a token_gone row. Read its eligibility in
                # the same writer transaction as the update, serial with retirement.
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            result = store.handle(conn, operation, owner, activity_id, payload, int(self.clock()), pending)
            if operation == "put" and result["body"].get("activity"):
                store.update(conn, activity_id, authorization_json=json.dumps(authorization))
            return result

    def _rows(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(tracks).where(tracks.c.status == "tracking")).mappings()]

    async def tick(self) -> None:
        for row in await asyncio.to_thread(self._rows):
            async with self._lock:
                pending = self.pending_question(row["session_id"])
                prepared = await self._joined_worker(self._prepare, row["activity_id"], pending)
                if prepared is None:
                    continue
                body, subscription = prepared
                # The lock is held through the bounded send, but no DB transaction is.
                # Shield/join the worker on cancellation: a replacement publisher may not
                # overtake a still-running HTTP request during controller shutdown.
                await self._joined_worker(self._deliver, row["activity_id"], subscription, body)

    @staticmethod
    async def _joined_worker(operation, *args):
        """Keep the controller lock until a cancelled request's DB/HTTP worker finishes."""
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _prepare(self, activity_id, pending):
        now = int(self.clock())
        with self.engine.begin() as conn:
            row = store.get(conn, activity_id)
            if row is None or row["status"] != "tracking":
                return None
            if now >= row["expires_at"]:
                store.stop(conn, activity_id, "expired", now)
                return None
            subscription = store.subscription(conn, row["owner_user_key"], row["push_registration_id"])
            if subscription is None:
                store.stop(conn, activity_id, "registration_unavailable", now)
                return None
            from storage.workbench_sessions_service import get_session

            context = None
            if row["owner_user_key"] != "local":
                from core.web_push_notifications import (
                    _resolve_owner_authorization_decisions,
                    WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA,
                    WEB_PUSH_DISPOSITION_REVOKED,
                )
                decisions = _resolve_owner_authorization_decisions(
                    {
                        WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA: [json.loads(row["authorization_json"] or "null")],
                    },
                    allow_sync_retry=False,
                )
                decision = decisions.get(row["owner_user_key"])
                if decision is None or not decision.authorized:
                    if decision is not None and decision.disposition == WEB_PUSH_DISPOSITION_REVOKED:
                        store.stop(conn, activity_id, "session_unavailable", now)
                    return None
                context = decision.context
            try:
                get_session(conn, row["session_id"], authorization_context=context)
            except LookupError:
                store.stop(conn, activity_id, "session_unavailable", now)
                return None
            state = store.snapshot(conn, row["session_id"], now, pending, row["turn_id"])
            queued = json.loads(row["pending_state"]) if row["pending_state"] else None
            if state is None:
                store.stop(conn, activity_id, "turn_unavailable", now)
                return None
            replace = (
                (queued is not None and queued["ended_at"] is None and queued["phase"] != state["phase"])
                or (queued is None and row["last_phase"] != state["phase"])
            )
            heartbeat_due = now - max(row["last_timestamp"], self._started_at) >= 900
            if replace or (queued is None and heartbeat_due):
                queued = {**state, "priority": 10 if replace else 5}
                store.update(conn, activity_id, pending_state=json.dumps(queued), retry_count=0, retry_at=0)
                row["retry_at"] = 0
            if queued is None or now < row["retry_at"]:
                return None
            # Keep the queued phase (including an end) while the phone is paused.
            # Only missing enrollment ends tracking; a pause reserves no relay timestamp.
            subscription = web_push_service.get_deliverable_by_endpoint(
                conn, endpoint=subscription["endpoint"], user_key=row["owner_user_key"]
            )
            if subscription is None:
                return None
            terminal = queued["ended_at"] is not None
            stamp = max(now, row["last_timestamp"] + 1)
            confirmed_at = max(now, queued["started_at"])
            body = {
                "activity_token": row["activity_token"],
                "event": "end" if terminal else "update",
                "timestamp": stamp,
                "priority": queued["priority"],
                "phase": queued["phase"],
                "started_at": queued["started_at"],
                "confirmed_at": confirmed_at,
            }
            if terminal:
                body.update(ended_at=queued["ended_at"], dismiss_at=queued["ended_at"] + 3600)
            else:
                body["stale_at"] = confirmed_at + 2400
                if queued["priority"] == 10 and queued["phase"] == "needsYou":
                    body["alert"] = "needsYou"
            # Persist the pending state and reserve a strictly increasing timestamp before
            # IO. An unanswered send can then survive a process restart without being lost.
            store.update(conn, activity_id, last_timestamp=stamp, updated_at=now)
            return body, subscription

    @staticmethod
    def _current_send(conn, activity_id, body):
        # The controller lock does not cover UI/CLI retirement. Take the DB
        # writer lock only after relay IO, through the result commit.
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        row = store.get(conn, activity_id)
        if (row and row['status'] == 'tracking' and row['last_timestamp'] == body['timestamp']
                and row['activity_token'] == body['activity_token']):
            return row
        return None

    def _deliver(self, activity_id, subscription, body):
        now = int(self.clock())
        try:
            self.sender(subscription=subscription, body=body)
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            with self.engine.begin() as conn:
                row = self._current_send(conn, activity_id, body)
                if row is None:
                    return
                if status == 410:
                    try:
                        result = response.json()
                    except ValueError:
                        result = None
                    token_gone = isinstance(result, dict) and result.get("error") == "activity_token_gone"
                    store.stop(conn, activity_id, "token_gone" if token_gone else "registration_gone", now)
                elif status == 502 or (status is not None and 400 <= status < 500 and status != 429):
                    store.stop(conn, activity_id, "relay_rejected", now)
                else:
                    count = min(4, row["retry_count"] + 1)
                    # Unknown server/protocol failures retain the latest state too, but
                    # only the contracted transient errors get the three rapid retries.
                    rapid = status in {None, 429, 503} and count < 4
                    delay = retry_after(response, now) if rapid else max(900, retry_after(response, now))
                    store.update(conn, activity_id, retry_count=count, retry_at=now + delay, updated_at=now)
            # HTTP exception repr contains the registration URL. Do not log it or tokens.
            logger.warning("Live Activity delivery failed status=%s", status)
            return
        with self.engine.begin() as conn:
            if self._current_send(conn, activity_id, body) is None:
                return
            store.update(
                conn,
                activity_id,
                last_phase=body["phase"],
                confirmed_at=body["confirmed_at"],
                ended_at=body.get("ended_at"),
                status="ended" if body["event"] == "end" else "tracking",
                pending_state=None,
                retry_count=0,
                retry_at=0,
                updated_at=now,
            )


def retry_after(response, now: int) -> int:
    value = response.headers.get("retry-after", "") if response is not None else ""
    try:
        return max(1, int(value))
    except ValueError:
        try:
            return max(1, int(parsedate_to_datetime(value).timestamp()) - now)
        except (ValueError, TypeError, OverflowError):
            return 5


def controller_pending_question(controller, session_id: str) -> bool:
    """Read the controller's registered question handlers, never UI-process memory."""
    service = getattr(controller, "agent_service", None)
    for gate in getattr(service, "_turn_gates", {}).values():
        request = getattr(gate, "request", None)
        if request is None or not getattr(gate, "token", None):
            continue
        context = request.context
        payload = context.platform_specific or {}
        sid = payload.get("agent_session_id") or payload.get("workbench_session_id")
        if sid != session_id:
            continue
        agent = getattr(gate, "agent", None) or getattr(service, "agents", {}).get(gate.backend)
        handler = getattr(agent, "_question_handler", None)
        if handler is not None and handler.get_pending(request.base_session_id) is not None:
            return True
    interactions = getattr(controller, "computer_script_interactions", None)
    return bool(interactions and interactions.has_pending_question(session_id))


def controller_service(controller) -> LiveActivityService:
    service = getattr(controller, "live_activity_service", None)
    if service is None:
        service = LiveActivityService(pending_question=lambda sid: controller_pending_question(controller, sid))
        controller.live_activity_service = service
    return service
