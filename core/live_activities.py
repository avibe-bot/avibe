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
from storage import live_activity_service as store
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
            if row["owner_user_key"] != "local":
                from core.web_push_notifications import (
                    _resolve_owner_authorization_decisions,
                    WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA,
                )
                from storage.workbench_sessions_service import get_session

                decisions = _resolve_owner_authorization_decisions(
                    {
                        WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA: [json.loads(row["authorization_json"] or "null")],
                    },
                    allow_sync_retry=False,
                )
                decision = decisions.get(row["owner_user_key"])
                if decision is None or not decision.authorized:
                    return None
                try:
                    get_session(conn, row["session_id"], authorization_context=decision.context)
                except LookupError:
                    store.stop(conn, activity_id, "session_unavailable", now)
                    return None
            state = store.snapshot(conn, row["session_id"], now, pending, row["turn_id"])
            if state is None:
                store.stop(conn, activity_id, "turn_unavailable", now)
                return None
            changed = state["phase"] != row["last_phase"]
            if changed and row["retry_phase"] == state["phase"] and (row["retry_count"] >= 4 or now < row["retry_at"]):
                return None
            if not changed and row["retry_phase"] is not None:
                store.update(conn, activity_id, retry_phase=None, retry_count=0, retry_at=0)
            if not changed and now - row["last_timestamp"] < 900:
                return None
            terminal = state["ended_at"] is not None
            stamp = max(now, row["last_timestamp"] + 1)
            body = {
                "activity_token": row["activity_token"],
                "event": "end" if terminal else "update",
                "timestamp": stamp,
                "priority": 10 if changed else 5,
                "phase": state["phase"],
                "started_at": state["started_at"],
                "confirmed_at": state["confirmed_at"],
            }
            if terminal:
                body.update(ended_at=state["ended_at"], dismiss_at=state["ended_at"] + 3600)
            else:
                body["stale_at"] = state["confirmed_at"] + 2400
                if changed and state["phase"] == "needsYou":
                    body["alert"] = "needsYou"
            # Reserve before IO, including rejected sends, so a restarted worker never
            # reuses a timestamp. A failed heartbeat is not retried.
            store.update(conn, activity_id, last_timestamp=stamp, updated_at=now)
            return body, subscription

    def _deliver(self, activity_id, subscription, body):
        now = int(self.clock())
        try:
            self.sender(subscription=subscription, body=body)
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            with self.engine.begin() as conn:
                if status == 410:
                    store.stop(conn, activity_id, "relay_gone", now)
                elif body["priority"] == 10:
                    row = store.get(conn, activity_id)
                    count = row["retry_count"] + 1 if row["retry_phase"] == body["phase"] else 1
                    store.update(
                        conn,
                        activity_id,
                        retry_phase=body["phase"],
                        retry_count=count if status in {429, 503} else 4,
                        retry_at=now + retry_after(response, now),
                        updated_at=now,
                    )
            # HTTP exception repr contains the registration URL. Do not log it or tokens.
            logger.warning("Live Activity delivery failed status=%s", status)
            return
        with self.engine.begin() as conn:
            store.update(
                conn,
                activity_id,
                last_phase=body["phase"],
                confirmed_at=body["confirmed_at"],
                ended_at=body.get("ended_at"),
                status="ended" if body["event"] == "end" else "tracking",
                retry_phase=None,
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
