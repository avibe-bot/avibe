"""Idle-delegate reporting on the existing durable RuntimeWork supervisor."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select, update

from core.runtime_work import RuntimeWorkItem, RuntimeWorkLane
from storage.agent_session_rows import reserve_write_lock
from storage.background import enqueue_run_in_connection, run_update_event_transaction
from storage.models import agent_sessions, session_delegations
from storage.resource_access_service import RESOURCE_USER_CONTEXT_METADATA_KEY
from storage.session_delegations import IDLE_NOTICE_ACTOR_PREFIX, candidate_query, eligible_pair, latest_reply

if TYPE_CHECKING:
    from core.scheduled_tasks import ScheduledTaskService


def notice_message(session_id: str, reply: str) -> str:
    excerpt = " ".join(reply.split())
    if len(excerpt) > 300:
        excerpt = excerpt[:299] + "…"
    # The id is runtime-owned. Bot Sessions use the same address as ordinary
    # Harness report hints; other Sessions keep the explicit --session-id form.
    from core.agent_input import _address

    _, command = _address(session_id)
    return f"Last reply: {excerpt or '(no reply recorded)'}\nFollow up: {command}"


class DelegationSafetyNet:
    def __init__(self, service: ScheduledTaskService) -> None:
        self.service = service

    def scan(self, *, limit: int, occupied: frozenset[str], cursor: str | None) -> tuple[list[RuntimeWorkItem], bool]:
        store = self.service.request_store.sqlite_backend
        if store is None:
            return [], False
        with store.engine.connect() as conn:
            rows = list(conn.execute(candidate_query(limit=limit + 1, cursor=cursor, occupied=occupied)).mappings())
            items = [RuntimeWorkItem(
                str(row["run_id"]), dict(row), rearm_after_process=False,
                cursor_only=eligible_pair(conn, dict(row)) is None,
            ) for row in rows[:limit]]
        return items, len(rows) > limit

    async def process(self, item: RuntimeWorkItem) -> bool:
        sent = await self.service._run_runtime_sync(self._notify, item.observation)
        if sent:
            self.service._wake_runtime_work(RuntimeWorkLane.REQUESTS)
        return True

    def _notify(self, observation: dict[str, Any]) -> bool:
        from core.message_priority import delivery_intent_for_trigger
        from core.scheduled_tasks import UnresolvableSessionTarget, resolve_session_id_target

        store = self.service.request_store.sqlite_backend
        if store is None:
            return False
        # Do routing I/O before taking the writer. Recheck Session existence and
        # eligibility under it; the request dispatcher revalidates its authority.
        try:
            target = resolve_session_id_target(observation["sender_session_id"], db_path=store.db_path)
        except UnresolvableSessionTarget:
            target = None
        with run_update_event_transaction(store.engine) as conn:
            reserve_write_lock(conn)
            pair = eligible_pair(conn, observation)
            if pair is None:
                return False
            if pair.get("unavailable"):
                conn.execute(update(session_delegations).where(
                    session_delegations.c.sender_session_id == pair["sender_session_id"],
                    session_delegations.c.recipient_session_id == pair["recipient_session_id"],
                ).values(state="unavailable"))
                return False
            if target is None:
                return False
            recipient = str(pair["recipient_session_id"])
            title = conn.execute(select(agent_sessions.c.title).where(agent_sessions.c.id == recipient)).scalar()
            metadata = {
                "source_session_id": recipient,
                "source_session_title": title or recipient,
                "delivery_intent": delivery_intent_for_trigger("callback"),
            }
            authority = pair["metadata"].get(RESOURCE_USER_CONTEXT_METADATA_KEY)
            if isinstance(authority, dict):
                metadata[RESOURCE_USER_CONTEXT_METADATA_KEY] = authority
            now = datetime.now(timezone.utc).isoformat()
            notice_id = uuid4().hex[:12]
            enqueue_run_in_connection(conn, {
                "id": notice_id, "run_type": "agent_run", "status": "queued",
                "source_kind": "callback", "source_actor": IDLE_NOTICE_ACTOR_PREFIX + pair["run_id"],
                "parent_run_id": pair["run_id"], "session_policy": "existing",
                "session_id": target.session_id, "legacy_session_key": target.session_key.to_key(),
                "agent_id": target.agent_id, "agent_name": target.agent_name, "agent_backend": target.agent_backend,
                "model": target.model, "reasoning_effort": target.reasoning_effort,
                "prompt": notice_message(recipient, latest_reply(conn, recipient)),
                "metadata_json": json.dumps(metadata), "created_at": now, "updated_at": now,
            })
            # Once and outbox commit together. The queued notice now wakes A;
            # it is not an unanswered delegation from B to A.
            conn.execute(update(session_delegations).where(
                session_delegations.c.sender_session_id == pair["sender_session_id"],
                session_delegations.c.recipient_session_id == recipient,
            ).values(state="notified", notice_run_id=notice_id, updated_at=now))
        return True
