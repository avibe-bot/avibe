"""The latest accepted Harness exchange for each directed pair of Sessions.

This is a projection, not reply attribution: any Agent message answers the reverse
pair. Runtime reports answer a pair without asking for a report in return.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, exists, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from storage.delivery_states import DELIVERY_STATE_MATRIX
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries,
    session_delegations,
    session_turns,
)

IDLE_NOTICE_ACTOR_PREFIX = "delegation-idle:"
#: ``state_meta`` key: when this instance first ran receipts for ended Turns. A
#: dispatched Turn that ended before it is history, never answered with a receipt:
#: 3.2.8rc2 sent 24 receipts for dispatches up to ten days old on its first scan.
RECEIPT_WATERMARK_KEY = "delegation_receipts_since"
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
        reverse = update(session_delegations).where(
            session_delegations.c.sender_session_id == recipient,
            session_delegations.c.recipient_session_id == sender,
            session_delegations.c.state.in_(("open", "notified", "answered")),
        )
        actor = str(run["source_actor"] or "")
        if run["source_kind"] == "callback" and actor.startswith(IDLE_NOTICE_ACTOR_PREFIX):
            # A receipt answers only the dispatch it was sent for, not a newer one (#4014 review).
            reverse = reverse.where(session_delegations.c.run_id == actor[len(IDLE_NOTICE_ACTOR_PREFIX):])
        answered = conn.execute(reverse.values(state="answered", updated_at=at)).rowcount
        if _object(run["metadata_json"]).get("no_reply") is True:
            # The run is still delivered and traceable, but it is informational:
            # it answers an existing reverse delegation without opening another.
            continue
        # Once B answers A, further progress/final/supplemental messages stay
        # answers. Only A sending again rearms A→B; wording/turns are not judged.
        if run["source_kind"] != "agent" or answered:
            continue
        values = dict(
            sender_session_id=sender, recipient_session_id=recipient,
            message_id=message_id, run_id=run["id"], state="open", notice_run_id=None, updated_at=at,
        )
        conn.execute(insert(session_delegations).values(**values).on_conflict_do_update(
            index_elements=[session_delegations.c.sender_session_id, session_delegations.c.recipient_session_id],
            set_={key: value for key, value in values.items() if key not in {"sender_session_id", "recipient_session_id"}},
        ))


def ensure_receipt_watermark(conn: Connection) -> str:
    """The receipt watermark, set to now the first time; idempotent."""
    import json as _json

    from storage.message_deliveries import turn_now_iso
    from storage.models import state_meta

    now = turn_now_iso()
    conn.execute(insert(state_meta).values(
        key=RECEIPT_WATERMARK_KEY, value_json=_json.dumps(now), updated_at=now,
    ).on_conflict_do_nothing(index_elements=[state_meta.c.key]))
    return receipt_watermark(conn) or now


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def receipt_watermark(conn: Connection) -> str | None:
    import json as _json

    from storage.models import state_meta

    raw = conn.execute(
        select(state_meta.c.value_json).where(state_meta.c.key == RECEIPT_WATERMARK_KEY)
    ).scalar_one_or_none()
    try:
        value = _json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, str) and value else None


def has_dispatch_pair(conn: Connection, sender_session_id: str, recipient_session_id: str) -> bool:
    """Whether this accepted dispatch direction has existed, including closed pairs.

    For an incoming B→A message, probe (A, B) to recognize A's own delegate.
    Queued/unaccepted sends and ordinary reverse answers do not create a pair.
    """
    return _has_rows(conn, select(session_delegations.c.sender_session_id).where(
        session_delegations.c.sender_session_id == sender_session_id,
        session_delegations.c.recipient_session_id == recipient_session_id,
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
    """Session-keyed probes for work the recipient is still doing or about to do.

    Only a live or waiting Turn, queued input, a live Run for it, or a report it
    is sending count. A Watch or Task it armed, or a delegation of its own still
    open, does not: each can wait for hours, and the dispatcher would hear nothing
    meanwhile (11-15 stall retro). The receipt says the dispatched Turn ended, which
    is true however the recipient arranged to continue.
    """
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
    )


def pending_return_queries(sender: str, recipient: str, run_id: str | None = None):
    """A return B→A still on its way defers the notice, not just the latest Run's callback.

    "On its way" means not yet accepted by A: once A accepted it, A has it, and A's
    own live Turn must not hold back a receipt for a newer dispatch (#4014 review).
    An older callback can share a turn with a newer no-callback message. Live
    callback children are bounded by A's live Runs before their metadata is read.
    An idle receipt counts only for the dispatch it was sent for (``run_id``).
    """
    callback_mode = case((func.json_valid(agent_runs.c.metadata_json) == 1,
                          func.json_extract(agent_runs.c.metadata_json, "$.callback_mode")))
    source_session = case((func.json_valid(agent_runs.c.metadata_json) == 1,
                           func.json_extract(agent_runs.c.metadata_json, "$.source_session_id")))
    not_accepted = ~exists(select(message_deliveries.c.id).where(
        message_deliveries.c.id == agent_runs.c.delivery_id, message_deliveries.c.state == "accepted",
    ))
    own_receipt = or_(
        ~agent_runs.c.source_actor.startswith(IDLE_NOTICE_ACTOR_PREFIX),
        agent_runs.c.source_actor == f"{IDLE_NOTICE_ACTOR_PREFIX}{run_id or ''}",
    )
    return (
        # A report B is sending back to A has not entered the pair projection until
        # A accepts it; it is a return in flight. B's Runs to anyone else are not:
        # B's own delegate may work for hours (#4014 review).
        select(agent_runs.c.id).where(
            agent_runs.c.source_kind == "agent", agent_runs.c.source_actor == recipient,
            agent_runs.c.session_id == sender, agent_runs.c.status.in_(_LIVE_RUN_STATUSES), not_accepted,
        ),
        select(agent_runs.c.id).where(
            agent_runs.c.session_id == recipient, agent_runs.c.callback_session_id == sender,
            agent_runs.c.callback_status == "pending",
            or_(func.coalesce(callback_mode, "") != "failure",
                agent_runs.c.status.in_(("failed", "canceled", "cancelled", "interrupted"))),
        ),
        select(agent_runs.c.id).where(
            agent_runs.c.session_id == sender, agent_runs.c.status.in_(_LIVE_RUN_STATUSES),
            agent_runs.c.source_kind == "callback", source_session == recipient, not_accepted, own_receipt,
        ),
    )


def eligible_pair(
    conn: Connection, observation: dict[str, Any], *, sync_run_ids: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Recheck the pair and all wake-up facts under the caller's writer lock."""
    pair = conn.execute(select(session_delegations).where(
        session_delegations.c.sender_session_id == observation["sender_session_id"],
        session_delegations.c.recipient_session_id == observation["recipient_session_id"],
        session_delegations.c.run_id == observation["run_id"], session_delegations.c.state == "open",
    )).mappings().first()
    if pair is None:
        return None
    run = conn.execute(select(
        agent_runs.c.metadata_json, agent_runs.c.status, agent_runs.c.cancel_requested, agent_runs.c.created_at,
    ).where(agent_runs.c.id == pair["run_id"])).mappings().first()
    if run is None:
        return None
    metadata = _object(run["metadata_json"])
    if metadata.get("no_reply") is True:
        # An older same-epoch binary can open this pair during rollback.
        # Retire it even when pending work would otherwise defer the receipt.
        return {**dict(pair), "unavailable": True}
    turn = conn.execute(dispatched_turn_query(str(pair["run_id"]))).mappings().first()
    watermark = _instant(receipt_watermark(conn))
    if watermark is None:
        return None  # not armed yet: the first scan sets it
    ended = _instant(turn["terminal_at"]) if turn is not None and turn["state"] == "terminal" else None
    if ended is None and turn is None:
        ended = _instant(pair["updated_at"])  # no Turn link: the dispatch's own acceptance
    if ended is not None and ended < watermark:
        # History: this dispatch ended before this instance sent receipts at all.
        return {**dict(pair), "unavailable": True}
    if _has_rows(conn, select(agent_runs.c.id).where(
        agent_runs.c.source_kind == "agent", agent_runs.c.source_actor == pair["recipient_session_id"],
        agent_runs.c.session_id == pair["sender_session_id"], agent_runs.c.created_at > run["created_at"],
        agent_runs.c.status.in_(("succeeded", "completed")),
    )):
        # The recipient has since written back to the dispatcher.
        return {**dict(pair), "answered": True}
    recipient = pair["recipient_session_id"]
    for session_id in (pair["sender_session_id"], recipient):
        session = conn.execute(select(agent_sessions.c.status).where(agent_sessions.c.id == session_id)).first()
        if session is None or session.status == "archived":
            return {**dict(pair), "unavailable": True}
    if any(_has_rows(conn, query) for query in pending_wake_queries(recipient)):
        return None
    # These are live controller waiters, not a durable promise which could
    # strand the pair after a restart. Probe each by PK before the terminal-read
    # acknowledgement below, closing the poll-versus-idle-notice race.
    for run_id in sync_run_ids:
        if _has_rows(conn, select(agent_runs.c.id).where(
            agent_runs.c.id == run_id, agent_runs.c.source_kind == "agent",
            agent_runs.c.source_actor == pair["sender_session_id"], agent_runs.c.session_id == recipient,
        )):
            return None
    if any(_has_rows(conn, query) for query in pending_return_queries(
        pair["sender_session_id"], recipient, str(pair["run_id"]),
    )):
        return None
    if run["cancel_requested"] or str(run["status"] or "") in {"canceled", "cancelled"}:
        # The dispatcher called it off; its failure notice, not a receipt, answers it.
        return {**dict(pair), "unavailable": True}
    return {**dict(pair), "metadata": metadata}


def acknowledge_sync_return(engine, run_id: str) -> None:
    """A waiting CLI read an accepted Run's terminal result: a return, not a request.

    A failure before acceptance says nothing about another open delegation. Once
    accepted, any return closes the pair, without matching replies to its latest Run.
    """
    from storage.agent_session_rows import reserve_write_lock
    from storage.background import run_update_event_transaction

    with run_update_event_transaction(engine) as conn:
        reserve_write_lock(conn)
        run = conn.execute(select(
            agent_runs.c.source_actor, agent_runs.c.session_id,
        ).select_from(agent_runs.join(message_deliveries, agent_runs.c.delivery_id == message_deliveries.c.id)).where(
            agent_runs.c.id == run_id, agent_runs.c.source_kind == "agent",
            agent_runs.c.status.in_(("succeeded", "failed", "canceled")), message_deliveries.c.state == "accepted",
        )).mappings().first()
        if run is not None:
            conn.execute(update(session_delegations).where(
                session_delegations.c.sender_session_id == run["source_actor"],
                session_delegations.c.recipient_session_id == run["session_id"],
                session_delegations.c.state.in_(("open", "notified")),
            ).values(state="answered", updated_at=datetime.now(timezone.utc).isoformat()))


def dispatched_turn_query(run_id: str):
    """The Turn that took a dispatched Run's input: three primary-key lookups."""
    return (
        select(
            session_turns.c.id, session_turns.c.state, session_turns.c.terminal_at,
            session_turns.c.terminal_evidence_json,
        )
        .select_from(
            agent_runs.join(message_deliveries, message_deliveries.c.id == agent_runs.c.delivery_id)
            .join(session_turns, session_turns.c.id == message_deliveries.c.turn_id)
        )
        .where(agent_runs.c.id == run_id)
    )


def dispatched_turn_reply(conn: Connection, run_id: str) -> str:
    """The last reply of the Turn that took the dispatched Run's input, or "".

    Not the Session's latest reply: that can be hours old and about something
    else (11-15 retro, 10:08 receipt quoting 04:33).
    """
    from storage.message_deliveries import turn_last_reply

    turn = conn.execute(dispatched_turn_query(run_id)).mappings().first()
    if turn is None:
        return ""
    # The final reply a Turn ends with is in its terminal evidence; a Turn that ends
    # without one records the last reply it delivered (``record_turn_reply``).
    final = _object(turn["terminal_evidence_json"]).get("result_text")
    return str(final or turn_last_reply(conn, str(turn["id"])) or "")
