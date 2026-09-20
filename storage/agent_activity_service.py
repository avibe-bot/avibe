"""Turn-grouped agent activity for the Web Chat Activity panel.

Composes the two persisted trace sources into per-turn groups:

* interim ``assistant`` messages (``messages`` table, ``type='assistant'``), and
* activity and migrated terminal events from ``agent_events``.

A *turn* ends at the agent's terminal reply (``result`` / ``error`` /
backend-failure ``notify``) or, when a new turn starts without one, is reported
as ``interrupted``. Durable input roles and start boundaries come from the
Delivery-to-Turn ownership graph: an initial Delivery opens its accepted Turn,
while accepted steer participants remain inside that Turn. Legacy/non-durable
rows fall back to transcript chronology. Message/event rows persist whole-second
``created_at``, but both also mint ids with a MICROSECOND clock prefix
(``<pfx>_<15-hex microsecond epoch><uuid8>``), so the merge sorts by
that decoded microsecond, recovering the true emission order ACROSS tables (a fast
turn's tool call before its same-second terminal; one turn's terminal before the
next turn's same-second opener). Durable Turn terminals retain their own
subsecond timestamp because they do not have a clock-bearing id. A phase
tiebreak (turn-start < activity < terminal) only applies when the microsecond
can't be decoded (format drift), and the whole-second ``created_at`` still
bounds the event scan.

Each group is keyed by the id of its first activity row (stable across summary
and detail reads). ``anchor_message_id`` is the transcript message the chip
renders against: the terminal reply for done/failed turns, or the next turn's
opening message for an interrupted turn (``None`` when the interrupted turn is
the last thing in the session — the chip trails the transcript).

Reads are bounded (recent tail) so a pathological session never triggers an
unbounded scan; the Chat loads the recent transcript first, so the recent turns
are exactly the ones covered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from core.backend_failure import is_backend_failure_notification
from storage import agent_events_service, messages_service
from storage.models import message_deliveries, session_turns
from vibe.message_types import (
    activity_role_for,
    is_detached_completion,
    spec_for,
    types_with,
)

# Bound the scan. The Chat retains ~300 recent messages and pages older on
# demand; covering the most-recent MESSAGE_SCAN_LIMIT transcript messages (and
# EVENT_SCAN_LIMIT tool-call events) keeps every recent turn while capping work.
# Groups older than this window are omitted (documented, not silent — see the PR).
MESSAGE_SCAN_LIMIT = 500
EVENT_SCAN_LIMIT = 2000

# Message types that participate in turn structure: visible or hidden turn openers,
# terminals (result/error/notify/silent-marker), and interim assistant activity rows.
# Hidden lifecycle rows are fetched even though they are not in TRANSCRIPT_TYPES so
# the activity projection preserves the complete Turn boundary.
_CONDITIONAL_TERMINAL_TYPES = types_with("terminalWhenEvents")
_TRANSCRIPT_ACTIVITY_TYPES = tuple(
    message_type
    for message_type in types_with("transcript")
    if spec_for(message_type)["activityRole"] != "none"
    or spec_for(message_type)["terminalWhenEvents"]
    or spec_for(message_type)["detachedCompletion"]
)
_NON_TRANSCRIPT_START_TYPES = tuple(
    message_type
    for message_type in types_with("activityRole")
    if spec_for(message_type)["activityRole"] == "turn_start"
    and message_type not in _TRANSCRIPT_ACTIVITY_TYPES
)
_NON_TRANSCRIPT_TERMINAL_TYPES = tuple(
    message_type
    for message_type in types_with("activityRole")
    if spec_for(message_type)["activityRole"] == "terminal"
    and message_type not in _TRANSCRIPT_ACTIVITY_TYPES
)
_ACTIVITY_TYPES = tuple(
    message_type
    for message_type in types_with("activityRole")
    if spec_for(message_type)["activityRole"] == "activity"
)
_RELEVANT_MESSAGE_TYPES = (
    *_TRANSCRIPT_ACTIVITY_TYPES,
    *_NON_TRANSCRIPT_START_TYPES,
    *_NON_TRANSCRIPT_TERMINAL_TYPES,
    *_ACTIVITY_TYPES,
)


def _parse_ts(value: Optional[str]) -> datetime:
    """Parse an ISO timestamp from either table into an aware UTC datetime.

    Both tables currently write ``...Z`` (whole seconds); normalize the trailing
    ``Z`` and assume UTC when no offset is present, and tolerate a fractional /
    offset form too (future-proofing). Unparseable values sort first.
    """
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_ms(started_iso: Optional[str], ended_iso: Optional[str]) -> Optional[int]:
    if not started_iso or not ended_iso:
        return None
    delta = (_parse_ts(ended_iso) - _parse_ts(started_iso)).total_seconds() * 1000.0
    if delta < 0:
        return 0
    return int(delta)


def _is_terminal(msg_type: Any, author: Any, metadata: Optional[dict]) -> bool:
    """Whether an agent message legally CLOSES a turn.

    Terminals: a visible ``result``/``error`` reply, a ``backend_failure`` ``notify``
    diagnostic, OR — when the turn produced nothing user-visible (a ``<silent>``-
    stripped/empty final, or a reply-less bookkeeping turn) — the invisible ``silent``
    event persisted at the delivery chokepoint. Only cancel/Stop (no terminal at all)
    stays ``interrupted``.

    A PLAIN ``notify`` is deliberately NOT terminal: agents emit mid-turn notify rows
    that explicitly do not end the turn (e.g. Claude's model-refusal fallback), so
    treating every notify as terminal would split one turn into two groups. A genuine
    notify-only COMPLETION is instead closed by the ``silent`` marker (its turn still
    emits an empty final result at the chokepoint), not by the notify row.
    """
    if author != "agent":
        return False
    return activity_role_for(
        msg_type if isinstance(msg_type, str) else "",
        metadata,
    ) == "terminal"


def _outcome_status(terminal_outcome: Any) -> str:
    """Render a stored terminal outcome as an activity-group status.

    One mapper for both boundary kinds — the durable Turn row and the IM
    ``silent_terminal`` trace that stands in for one — so a settlement never means
    two different things depending on which surface recorded it. A turn that was
    canceled or never written its outcome is ``interrupted``, not ``done``: it
    ended without producing an answer, and the group chip should say so.
    """

    if terminal_outcome == "failed":
        return "failed"
    if terminal_outcome in {"canceled", "not_written"}:
        return "interrupted"
    return "done"


def _terminal_status(msg_type: Any, metadata: Optional[dict] = None) -> str:
    """done for a normal completion (result / silent marker); failed for an ``error``
    or a ``backend_failure`` notify."""
    if msg_type == "error":
        return "failed"
    if (
        msg_type in _CONDITIONAL_TERMINAL_TYPES
        and is_backend_failure_notification(msg_type, metadata)
    ):
        return "failed"
    return "done"


# Fallback tiebreak only (used when a row's microsecond id prefix can't be decoded,
# e.g. format drift): within a single turn the order is open → work → close.
_PHASE_RANK = {
    "turn_start": 0,
    "activity": 1,
    "boundary": 2,
    "detached_completion": 3,
    "terminal": 4,
    "ignore": 5,
}


def _emit_micros(row_id: Optional[str], ts: datetime) -> int:
    """The row's emission microsecond, decoded from the id's clock prefix.

    Both tables mint ids as ``<pfx>_<15-hex microsecond epoch><uuid8>`` (see
    ``messages_service`` / ``agent_events_service``), so this recovers the true
    sub-second emission order ACROSS tables — which whole-second ``created_at``
    cannot. Falls back to the parsed timestamp when an id doesn't match the format.
    """
    if row_id and len(row_id) >= 19 and row_id[3] == "_":
        try:
            return int(row_id[4:19], 16)
        except ValueError:
            pass
    return int(ts.timestamp() * 1_000_000)


def _event_emit_micros(event: dict[str, Any], ts: datetime) -> int:
    metadata = event.get("metadata") or {}
    legacy_message_id = metadata.get("legacy_message_id")
    return _emit_micros(
        str(legacy_message_id) if legacy_message_id else event.get("id"),
        ts,
    )


def _timeline(conn, session_id: str, *, include_text: bool) -> list[dict[str, Any]]:
    """Merge the recent tail of relevant messages + tool-call events into one
    chronologically-ordered list of classified items."""
    msgs = messages_service.list_session_messages(
        conn,
        session_id=session_id,
        limit=MESSAGE_SCAN_LIMIT,
        tail=True,
        types=_RELEVANT_MESSAGE_TYPES,
    )["messages"]
    events = agent_events_service.list_session_events(
        conn,
        session_id=session_id,
        event_types=("tool_call", "silent_terminal"),
        limit=EVENT_SCAN_LIMIT,
        newest_first=True,
    )
    message_ids = tuple(
        str(message["id"])
        for message in msgs
        if message.get("id") is not None
    )
    accepted_roles: dict[str, dict[str, Any]] = {}
    if message_ids:
        rows = conn.execute(
            select(
                message_deliveries.c.message_id,
                message_deliveries.c.id.label("delivery_id"),
                message_deliveries.c.materialized_at,
                session_turns.c.id.label("turn_id"),
                session_turns.c.initial_delivery_id,
                session_turns.c.started_at,
                session_turns.c.created_at.label("turn_created_at"),
            )
            .select_from(
                message_deliveries.join(
                    session_turns,
                    session_turns.c.id == message_deliveries.c.turn_id,
                )
            )
            .where(message_deliveries.c.state == "accepted")
            .where(message_deliveries.c.message_id.in_(message_ids))
        ).mappings()
        for row in rows:
            if row["message_id"] is None:
                continue
            key = str(row["message_id"])
            candidate = dict(row)
            current = accepted_roles.get(key)
            if current is None or candidate["delivery_id"] == candidate["initial_delivery_id"]:
                accepted_roles[key] = candidate

    items: list[dict[str, Any]] = []
    for msg in msgs:
        mtype = msg.get("type")
        author = msg.get("author")
        metadata = msg.get("metadata") or {}
        activity_role = activity_role_for(
            mtype if isinstance(mtype, str) else "",
            metadata,
        )
        accepted_role = accepted_roles.get(str(msg.get("id") or ""))
        if _is_terminal(mtype, author, metadata):
            kind = "terminal"
        elif is_detached_completion(
            mtype if isinstance(mtype, str) else "",
            metadata,
        ):
            kind = "detached_completion"
        elif activity_role == "turn_start":
            kind = (
                "turn_start"
                if accepted_role is None
                or accepted_role["delivery_id"]
                == accepted_role["initial_delivery_id"]
                else "ignore"
            )
        elif activity_role == "activity":
            kind = "activity"
        elif activity_role == "boundary":
            kind = "boundary"
        else:
            kind = "ignore"
        created_at = msg.get("created_at")
        if kind == "turn_start" and accepted_role is not None:
            created_at = (
                accepted_role.get("started_at")
                or accepted_role.get("materialized_at")
                or accepted_role.get("turn_created_at")
                or created_at
            )
        mts = _parse_ts(created_at)
        items.append(
            {
                "ts": mts,
                "sort": (
                    int(mts.timestamp() * 1_000_000)
                    if accepted_role is not None and kind == "turn_start"
                    else _emit_micros(msg.get("id"), mts)
                ),
                "rank": _PHASE_RANK[kind],
                "created_at": created_at,
                "kind": kind,
                "id": msg.get("id"),
                "mtype": mtype,
                "turn_id": str(
                    metadata.get("turn_id")
                    or (accepted_role or {}).get("turn_id")
                    or ""
                ).strip()
                or None,
                "is_transcript": bool(
                    spec_for(mtype if isinstance(mtype, str) else "")["transcript"]
                ),
                "row_kind": "assistant",
                "text": msg.get("text") if include_text else None,
                # The silent marker is a terminal that is INVISIBLE in the transcript,
                # so a group closing on it must anchor to the (visible) turn trigger
                # rather than the marker itself; ``terminal_status`` is resolved here so
                # ``notify`` failure/normal is decided with its metadata in hand.
                "is_silent": False,
                "terminal_status": (
                    _terminal_status(mtype, metadata)
                    if kind in {"boundary", "detached_completion", "terminal"}
                    else None
                ),
            }
        )
    # Bound events to the scanned message window: in a long session the 500-message
    # tail can start after some of the fetched events, and an event whose turn
    # boundary was NOT fetched would otherwise be grouped as pending and anchored to
    # the first visible turn — surfacing an earlier turn's tool calls above the wrong
    # message. Compare by the decoded microsecond sort key (not just the whole
    # second), so a same-second event emitted BEFORE the oldest scanned message is
    # dropped too.
    oldest_msg_sort = min((item["sort"] for item in items), default=None)
    for event in events:
        event_ts = _parse_ts(event.get("created_at"))
        event_sort = _event_emit_micros(event, event_ts)
        if oldest_msg_sort is not None and event_sort < oldest_msg_sort:
            continue
        if event.get("event_type") == "silent_terminal":
            terminal_outcome = (event.get("metadata") or {}).get(
                "terminal_outcome"
            )
            items.append(
                {
                    "ts": event_ts,
                    "sort": event_sort,
                    "rank": _PHASE_RANK["terminal"],
                    "created_at": event.get("created_at"),
                    "kind": "terminal",
                    "id": event.get("id"),
                    "mtype": "silent_terminal",
                    "turn_id": str(event.get("turn_id") or "").strip() or None,
                    "row_kind": "turn_terminal",
                    "text": None,
                    "is_silent": True,
                    # Same mapping as the durable-Turn branch below, because the
                    # silent marker IS the IM stand-in for one: a turn the service
                    # retired writes ``canceled`` here too, and rendering that as a
                    # green ``done`` would claim an answer that was never produced.
                    "terminal_status": _outcome_status(terminal_outcome),
                }
            )
            continue
        items.append(
            {
                "ts": event_ts,
                "sort": event_sort,
                "rank": _PHASE_RANK["activity"],
                "created_at": event.get("created_at"),
                "kind": "activity",
                "id": event.get("id"),
                "mtype": "tool_call",
                "turn_id": str(event.get("turn_id") or "").strip() or None,
                "row_kind": "tool_call",
                "text": event.get("text") if include_text else None,
            }
        )
    for turn in conn.execute(
        select(
            session_turns.c.id,
            session_turns.c.terminal_outcome,
            session_turns.c.terminal_at,
        )
        .where(session_turns.c.session_id == session_id)
        .where(session_turns.c.state == "terminal")
        .where(session_turns.c.terminal_outcome != "not_written")
        .where(session_turns.c.terminal_at.is_not(None))
        .order_by(
            func.julianday(session_turns.c.terminal_at).desc(),
            session_turns.c.id.desc(),
        )
        .limit(MESSAGE_SCAN_LIMIT)
    ).mappings():
        terminal_ts = _parse_ts(turn["terminal_at"])
        items.append(
            {
                "ts": terminal_ts,
                "sort": int(terminal_ts.timestamp() * 1_000_000),
                "rank": _PHASE_RANK["terminal"],
                "created_at": turn["terminal_at"],
                "kind": "terminal",
                "id": f"turn-terminal:{turn['id']}",
                "mtype": "turn_terminal",
                "turn_id": str(turn["id"] or "").strip() or None,
                "row_kind": "turn_terminal",
                "text": None,
                "is_silent": True,
                "terminal_status": _outcome_status(turn["terminal_outcome"]),
            }
        )
    # Sort by decoded emission microsecond (true cross-table order); the phase rank
    # is a fallback for undecodable ids, and the id a final deterministic tiebreak.
    items.sort(key=lambda item: (item["sort"], item["rank"], item["id"]))
    return items


def _make_group(
    pending: list[dict[str, Any]],
    *,
    status: str,
    anchor_id: Optional[str],
    anchor_position: str,
    open_turn: bool,
    started_iso: Optional[str],
    ended_iso: Optional[str],
    include_rows: bool,
    turn_id: Optional[str],
) -> dict[str, Any]:
    started = started_iso or pending[0]["created_at"]
    group: dict[str, Any] = {
        "id": pending[0]["id"],
        # A group is positioned relative to a transcript message that is AT OR BEFORE
        # the group's own end (never a future message): done/failed anchor to their
        # terminal reply (rendered BEFORE it — hug the reply from above); interrupted
        # anchor to the turn's trigger / the boundary before its activity (rendered
        # AFTER it). ``open_turn`` marks the last un-terminated turn — the only group
        # the frontend may promote into the tail live card while it is still running.
        "anchor_message_id": anchor_id,
        "anchor_position": anchor_position,
        "open": open_turn,
        "status": status,
        "steps": len(pending),
        "started_at": started,
        "ended_at": ended_iso,
        "duration_ms": _duration_ms(started, ended_iso),
        "_turn_id": turn_id,
    }
    if include_rows:
        group["rows"] = [
            {
                "id": item["id"],
                "kind": item["row_kind"],
                "text": item.get("text") or "",
                "created_at": item["created_at"],
                # Migrated event ids are hashes; only storage has the original clock.
                "order_micros": item["sort"],
            }
            for item in pending
        ]
    return group


def _build_groups(items: list[dict[str, Any]], *, include_rows: bool) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    turn_start_iso: Optional[str] = None
    turn_id: Optional[str] = None
    # Id of the most recent transcript-visible boundary (turn_start OR terminal). An
    # interrupted turn anchors BACKWARD to this — the boundary immediately before its
    # activity (its trigger) — so its chip is positioned by its OWN chronology and
    # never attaches to a future message (the ordering bug).
    last_boundary_id: Optional[str] = None
    for item in items:
        kind = item["kind"]
        if kind == "activity":
            if turn_id is None:
                turn_id = item.get("turn_id")
            pending.append(item)
        elif kind == "turn_start":
            if pending:
                # Activity with no terminal before a new turn opened → interrupted;
                # anchor AFTER the boundary that preceded this activity (its trigger),
                # NOT the next turn's opener. Not ``open`` — a later turn exists.
                groups.append(
                    _make_group(
                        pending,
                        status="interrupted",
                        anchor_id=last_boundary_id,
                        anchor_position="after",
                        open_turn=False,
                        started_iso=turn_start_iso,
                        ended_iso=pending[-1]["created_at"],
                        include_rows=include_rows,
                        turn_id=turn_id,
                    )
                )
                pending = []
            turn_start_iso = item["created_at"]
            turn_id = item.get("turn_id")
            if item["is_transcript"]:
                last_boundary_id = item["id"]
        elif kind == "boundary":
            if pending:
                groups.append(
                    _make_group(
                        pending,
                        status=item["terminal_status"],
                        anchor_id=item["id"],
                        anchor_position="before",
                        open_turn=False,
                        started_iso=turn_start_iso,
                        ended_iso=item["created_at"],
                        include_rows=include_rows,
                        turn_id=turn_id,
                    )
                )
                pending = []
            # This output completes the preceding visible work without ending the
            # logical Turn. Later activity belongs after this boundary and times
            # from it until the eventual terminal or interruption.
            turn_start_iso = item["created_at"]
            if item["is_transcript"]:
                last_boundary_id = item["id"]
        elif kind == "detached_completion":
            owner_turn_id = item.get("turn_id")
            pending_turn_ids = {
                pending_item.get("turn_id")
                for pending_item in pending
                if pending_item.get("turn_id")
            }
            has_unresolved_origin = any(
                group["status"] == "interrupted" for group in groups
            )
            provenance_matches_pending = bool(
                owner_turn_id
                and (
                    (
                        owner_turn_id == turn_id
                        and pending_turn_ids <= {owner_turn_id}
                    )
                    or (turn_id is None and pending_turn_ids == {owner_turn_id})
                )
            )
            owns_pending = bool(
                provenance_matches_pending
                # Recovered legacy activity may have no provenance on either side.
                # Only the sole unresolved group is safe to close chronologically.
                or (
                    owner_turn_id is None
                    and turn_id is None
                    and not pending_turn_ids
                    and not has_unresolved_origin
                )
            )
            if pending and owns_pending:
                groups.append(
                    _make_group(
                        pending,
                        status=item["terminal_status"],
                        anchor_id=item["id"],
                        anchor_position="before",
                        open_turn=False,
                        started_iso=turn_start_iso,
                        ended_iso=item["created_at"],
                        include_rows=include_rows,
                        turn_id=owner_turn_id,
                    )
                )
                pending = []
                turn_start_iso = None
                turn_id = None
            elif owner_turn_id:
                # A later Turn may already have interrupted the origin group. Repair
                # that group by provenance without consuming the newer Turn's rows.
                for group in reversed(groups):
                    if (
                        group.get("_turn_id") == owner_turn_id
                        and group["status"] == "interrupted"
                    ):
                        group.update(
                            status=item["terminal_status"],
                            anchor_message_id=item["id"],
                            anchor_position="before",
                            open=False,
                            ended_at=item["created_at"],
                            duration_ms=_duration_ms(
                                group.get("started_at"),
                                item["created_at"],
                            ),
                        )
                        break
        elif kind == "terminal":
            if pending:
                # A silent marker is invisible in the transcript, so its DONE group
                # anchors to the (visible) turn trigger AFTER it — never to the marker,
                # which the frontend can't position against (#935 backward-anchor). A
                # visible terminal (result/error/notify) anchors to itself, BEFORE it
                # (the chip hugs the reply from above).
                if item["is_silent"]:
                    anchor_id, anchor_position = last_boundary_id, "after"
                else:
                    anchor_id, anchor_position = item["id"], "before"
                groups.append(
                    _make_group(
                        pending,
                        status=item["terminal_status"],
                        anchor_id=anchor_id,
                        anchor_position=anchor_position,
                        open_turn=False,
                        started_iso=turn_start_iso,
                        ended_iso=item["created_at"],
                        include_rows=include_rows,
                        turn_id=turn_id,
                    )
                )
                pending = []
            turn_start_iso = None
            turn_id = None
            # Keep ``last_boundary_id`` on a TRANSCRIPT-VISIBLE row: a visible terminal
            # becomes the new boundary; the invisible silent marker does NOT (a later
            # turn must still anchor to a row the frontend can render).
            if not item["is_silent"]:
                last_boundary_id = item["id"]
        # kind == "ignore": leave pending + boundary + turn_start untouched
    if pending:
        # The last un-terminated turn. Anchor AFTER its trigger (never the tail); the
        # frontend renders it as an interrupted chip there, OR — while the turn is
        # still running — promotes it into the tail live card (``open``).
        groups.append(
            _make_group(
                pending,
                status="interrupted",
                anchor_id=last_boundary_id,
                anchor_position="after",
                open_turn=True,
                started_iso=turn_start_iso,
                ended_iso=pending[-1]["created_at"],
                include_rows=include_rows,
                turn_id=turn_id,
            )
        )
    for group in groups:
        group.pop("_turn_id", None)
    return groups


def list_turn_groups(conn, *, session_id: str) -> dict[str, Any]:
    """Summary of every activity group in the recent window: one entry per turn
    that produced ≥1 activity row, without the (potentially large) row text."""
    groups = _build_groups(_timeline(conn, session_id, include_text=False), include_rows=False)
    return {"groups": groups}


def get_turn_group(conn, *, session_id: str, group_id: str) -> Optional[dict[str, Any]]:
    """One group's full rows (interim assistant text + tool-call text), for the
    lazy expand. ``group_id`` is the group's first-activity-row id (from the
    summary). Returns ``None`` when no group matches."""
    groups = _build_groups(_timeline(conn, session_id, include_text=True), include_rows=True)
    for group in groups:
        if group["id"] == group_id:
            return group
    return None
