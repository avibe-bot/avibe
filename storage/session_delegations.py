"""The latest accepted Harness exchange for each directed pair of Sessions.

This is a projection, not reply attribution: any Agent message answers the reverse
pair. Runtime reports answer a pair without asking for a report in return.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import exists, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from storage.delivery_states import DELIVERY_STATE_MATRIX
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries,
    messages,
    run_definitions,
    session_delegations,
    session_turns,
)

IDLE_NOTICE_ACTOR_PREFIX = "delegation-idle:"
_PENDING_DELIVERIES = tuple(state for state, policy in DELIVERY_STATE_MATRIX.items() if policy.ordering != "terminal")
_LIVE_RUN_STATUSES = ("queued", "pending", "running", "processing")


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def record_accepted_exchange(conn: Connection, *, message_id: str, snapshot: dict[str, Any], at: str) -> None:
    """Called once, in the transaction inserting an accepted Harness Message.

    Merged/P1 inputs keep each Run's provenance. Read the Run itself rather than
    trusting text or display labels; user/Watch/Task/Vault messages never delegate.
    """
    if snapshot.get("source") != "harness":
        return
    metadata = _object(snapshot.get("metadata_json"))
    sources = metadata.get("merged_harness_sources")
    if not isinstance(sources, list):
        sources = [_object(metadata.get("scheduled_provenance")).get("platform_specific")]
    recipient = str(snapshot["session_id"])
    for source in sources:
        if not isinstance(source, dict) or source.get("task_trigger_kind") != "agent_run":
            continue
        run = conn.execute(select(
            agent_runs.c.id, agent_runs.c.run_type, agent_runs.c.session_id,
            agent_runs.c.source_kind, agent_runs.c.source_actor, agent_runs.c.metadata_json,
        ).where(agent_runs.c.id == source.get("task_execution_id"))).mappings().first()
        if run is None or run["run_type"] != "agent_run" or run["session_id"] != recipient:
            continue
        if run["source_kind"] == "agent":
            sender = run["source_actor"]
        elif run["source_kind"] == "callback":
            sender = _object(run["metadata_json"]).get("source_session_id")
        else:
            continue
        if not isinstance(sender, str) or sender == recipient or not conn.execute(
            select(agent_sessions.c.id).where(agent_sessions.c.id == sender)
        ).scalar_one_or_none():
            continue
        conn.execute(update(session_delegations).where(
            session_delegations.c.sender_session_id == recipient,
            session_delegations.c.recipient_session_id == sender,
        ).values(state="answered", updated_at=at))
        if run["source_kind"] != "agent":
            continue
        values = dict(
            sender_session_id=sender, recipient_session_id=recipient,
            message_id=message_id, run_id=run["id"], state="open", notice_run_id=None, updated_at=at,
        )
        conn.execute(insert(session_delegations).values(**values).on_conflict_do_update(
            index_elements=[session_delegations.c.sender_session_id, session_delegations.c.recipient_session_id],
            set_={key: value for key, value in values.items() if key not in {"sender_session_id", "recipient_session_id"}},
        ))


def candidate_query(*, limit: int, cursor: str | None = None, occupied: frozenset[str] = frozenset()):
    """Page only pending pairs, never the Run/Message history."""
    query = select(session_delegations).where(session_delegations.c.state == "open")
    if cursor:
        query = query.where(session_delegations.c.run_id > cursor)
    if occupied:
        query = query.where(~session_delegations.c.run_id.in_(occupied))
    return query.order_by(session_delegations.c.run_id).limit(limit)


def _has_rows(conn: Connection, query) -> bool:
    return bool(conn.execute(select(exists(query))).scalar())


def pending_wake_queries(session_id: str):
    """Session-keyed probes; retired/paused definitions are not future wake-ups."""
    return (
        select(session_turns.c.id).where(
            session_turns.c.session_id == session_id, text("state in ('starting', 'active')"),
        ),
        select(session_turns.c.id).where(
            session_turns.c.session_id == session_id, text("state = 'waiting'"),
        ),
        select(message_deliveries.c.id).where(
            message_deliveries.c.session_id == session_id, message_deliveries.c.state.in_(_PENDING_DELIVERIES),
        ),
        select(agent_runs.c.id).where(
            agent_runs.c.session_id == session_id, agent_runs.c.status.in_(_LIVE_RUN_STATUSES),
        ),
        select(run_definitions.c.id).where(
            run_definitions.c.session_id == session_id,
            run_definitions.c.definition_type.in_(("watch", "scheduled")),
            run_definitions.c.enabled == 1, run_definitions.c.deleted_at.is_(None), run_definitions.c.retired_at.is_(None),
        ),
        select(session_delegations.c.run_id).where(
            session_delegations.c.sender_session_id == session_id, session_delegations.c.state == "open",
        ),
    )


def eligible_pair(conn: Connection, observation: dict[str, Any]) -> dict[str, Any] | None:
    """Recheck the pair and all wake-up facts under the caller's writer lock."""
    pair = conn.execute(select(session_delegations).where(
        session_delegations.c.sender_session_id == observation["sender_session_id"],
        session_delegations.c.recipient_session_id == observation["recipient_session_id"],
        session_delegations.c.run_id == observation["run_id"], session_delegations.c.state == "open",
    )).mappings().first()
    if pair is None:
        return None
    recipient = pair["recipient_session_id"]
    for session_id in (pair["sender_session_id"], recipient):
        session = conn.execute(select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)).first()
        if session is None or session.status == "archived":
            return {**dict(pair), "unavailable": True}
    if any(_has_rows(conn, query) for query in pending_wake_queries(recipient)):
        return None
    run = conn.execute(select(
        agent_runs.c.status, agent_runs.c.metadata_json, agent_runs.c.callback_session_id,
        agent_runs.c.callback_status, agent_runs.c.callback_run_id,
    ).where(agent_runs.c.id == pair["run_id"])).mappings().first()
    if run is None:
        return None
    # The ordinary callback/failure-notice owner has first refusal. Enqueued is
    # not delivered: a failed return may still need this safety net.
    metadata = _object(run["metadata_json"])
    owes_callback = metadata.get("callback_mode") != "failure" or run["status"] in {"failed", "canceled", "cancelled", "interrupted"}
    if run["callback_session_id"] == pair["sender_session_id"] and owes_callback:
        if run["callback_status"] == "pending":
            return None
        if run["callback_run_id"]:
            callback = conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == run["callback_run_id"])).first()
            if callback is not None and callback.status in _LIVE_RUN_STATUSES:
                return None
    return {**dict(pair), "metadata": metadata}


def latest_reply_query(session_id: str, message_type: str):
    return select(messages.c.id, messages.c.created_at, messages.c.content_text).where(
        messages.c.session_id == session_id, messages.c.type == message_type,
    ).order_by(messages.c.created_at.desc(), messages.c.id.desc()).limit(1)


def latest_reply(conn: Connection, session_id: str) -> str:
    # One ordered seek per visible reply kind; an IN + ORDER BY needs to sort a
    # growing Session history. Hidden assistant/tool/thinking rows are not replies.
    replies = [row for kind in ("result", "error", "output") if (
        row := conn.execute(latest_reply_query(session_id, kind)).mappings().first()
    ) is not None]
    if not replies:
        return ""
    return str(max(replies, key=lambda row: (row["created_at"], row["id"]))["content_text"] or "")
