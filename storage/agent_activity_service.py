"""Turn-grouped agent activity for the Web Chat Activity panel.

Composes the two persisted trace sources into per-turn groups:

* interim ``assistant`` messages (``messages`` table, ``type='assistant'``), and
* ``tool_call`` events (``agent_events`` table, ``event_type='tool_call'``).

A *turn* is bounded by transcript markers rather than an id: it ends at the
agent's terminal reply (``result`` / ``error`` / backend-failure ``notify``) or,
when the user starts a new turn without one, is reported as ``interrupted``.
Grouping is chronological because ``messages`` carries no ``turn_id`` (only
``agent_events`` does). Both tables persist WHOLE-SECOND ``...Z`` timestamps, so a
same-second tool call and the terminal reply that followed it tie on time; the
merge therefore sorts by ``(parsed_timestamp, phase)`` where phase orders
turn-start < activity < terminal, keeping a same-second tool event inside the
completed turn instead of after its terminal. Timestamps are PARSED (not string-
compared) so the sort stays correct even if a future writer changes precision.

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

from storage import agent_events_service, messages_service

# Bound the scan. The Chat retains ~300 recent messages and pages older on
# demand; covering the most-recent MESSAGE_SCAN_LIMIT transcript messages (and
# EVENT_SCAN_LIMIT tool-call events) keeps every recent turn while capping work.
# Groups older than this window are omitted (documented, not silent — see the PR).
MESSAGE_SCAN_LIMIT = 500
EVENT_SCAN_LIMIT = 2000

# Message types that participate in turn structure: turn openers (user/harness),
# terminals (result/error/notify), and the interim assistant activity rows.
_RELEVANT_MESSAGE_TYPES = (
    "user",
    messages_service.HARNESS_TYPE,
    "result",
    "error",
    "notify",
    "assistant",
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
    """Mirror the frontend ``isTerminalAgentMessage`` predicate."""
    if author != "agent":
        return False
    if msg_type in ("result", "error"):
        return True
    if msg_type == "notify" and (metadata or {}).get("event") == "backend_failure":
        return True
    return False


def _terminal_status(msg_type: Any) -> str:
    return "done" if msg_type == "result" else "failed"


# Same-timestamp tiebreak: a turn opens, then its activity runs, then it closes —
# so at an equal (whole-second) timestamp order turn-start < activity < terminal.
# This keeps a same-second tool call inside the completed turn rather than sorting
# it after the terminal (which would orphan it to the next turn / an interrupted chip).
_PHASE_RANK = {"turn_start": 0, "activity": 1, "terminal": 2, "ignore": 3}


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
        event_types=("tool_call",),
        limit=EVENT_SCAN_LIMIT,
        newest_first=True,
    )

    items: list[dict[str, Any]] = []
    for msg in msgs:
        mtype = msg.get("type")
        author = msg.get("author")
        metadata = msg.get("metadata") or {}
        if _is_terminal(mtype, author, metadata):
            kind = "terminal"
        elif mtype in ("user", messages_service.HARNESS_TYPE):
            kind = "turn_start"
        elif mtype == "assistant" and metadata.get("source") != "show_page":
            # Show-Page transcript marks are also stored as ``assistant`` rows but
            # belong to the transcript, not the process log — never activity.
            kind = "activity"
        else:
            kind = "ignore"
        items.append(
            {
                "ts": _parse_ts(msg.get("created_at")),
                "rank": _PHASE_RANK[kind],
                "created_at": msg.get("created_at"),
                "kind": kind,
                "id": msg.get("id"),
                "mtype": mtype,
                "row_kind": "assistant",
                "text": msg.get("text") if include_text else None,
            }
        )
    # Bound events to the scanned message window: in a long session the 500-message
    # tail can start after some of the fetched events, and an event whose turn
    # boundary was NOT fetched would otherwise be grouped as pending and anchored to
    # the first visible turn — surfacing an earlier turn's tool calls above the wrong
    # message. Drop events that predate the oldest scanned message (parsed compare,
    # so the two tables' string forms never matter).
    oldest_msg_ts = min((item["ts"] for item in items), default=None)
    for event in events:
        event_ts = _parse_ts(event.get("created_at"))
        if oldest_msg_ts is not None and event_ts < oldest_msg_ts:
            continue
        items.append(
            {
                "ts": event_ts,
                "rank": _PHASE_RANK["activity"],
                "created_at": event.get("created_at"),
                "kind": "activity",
                "id": event.get("id"),
                "mtype": "tool_call",
                "row_kind": "tool_call",
                "text": event.get("text") if include_text else None,
            }
        )
    # Sort by (timestamp, phase): whole-second ties resolve to turn-start < activity
    # < terminal so a same-second tool call stays inside its completed turn.
    items.sort(key=lambda item: (item["ts"], item["rank"]))
    return items


def _make_group(
    pending: list[dict[str, Any]],
    *,
    status: str,
    anchor_id: Optional[str],
    started_iso: Optional[str],
    ended_iso: Optional[str],
    include_rows: bool,
) -> dict[str, Any]:
    started = started_iso or pending[0]["created_at"]
    group: dict[str, Any] = {
        "id": pending[0]["id"],
        "anchor_message_id": anchor_id,
        "status": status,
        "steps": len(pending),
        "started_at": started,
        "ended_at": ended_iso,
        "duration_ms": _duration_ms(started, ended_iso),
    }
    if include_rows:
        group["rows"] = [
            {
                "id": item["id"],
                "kind": item["row_kind"],
                "text": item.get("text") or "",
                "created_at": item["created_at"],
            }
            for item in pending
        ]
    return group


def _build_groups(items: list[dict[str, Any]], *, include_rows: bool) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    turn_start_iso: Optional[str] = None
    for item in items:
        kind = item["kind"]
        if kind == "activity":
            pending.append(item)
        elif kind == "turn_start":
            if pending:
                # Activity with no terminal before a new turn opened → interrupted;
                # anchor the chip against the opening message of the next turn.
                groups.append(
                    _make_group(
                        pending,
                        status="interrupted",
                        anchor_id=item["id"],
                        started_iso=turn_start_iso,
                        ended_iso=pending[-1]["created_at"],
                        include_rows=include_rows,
                    )
                )
                pending = []
            turn_start_iso = item["created_at"]
        elif kind == "terminal":
            if pending:
                groups.append(
                    _make_group(
                        pending,
                        status=_terminal_status(item["mtype"]),
                        anchor_id=item["id"],
                        started_iso=turn_start_iso,
                        ended_iso=item["created_at"],
                        include_rows=include_rows,
                    )
                )
                pending = []
            turn_start_iso = None
        # kind == "ignore": leave pending + turn_start untouched
    if pending:
        # Trailing interrupted turn (no following message): chip trails the transcript.
        groups.append(
            _make_group(
                pending,
                status="interrupted",
                anchor_id=None,
                started_iso=turn_start_iso,
                ended_iso=pending[-1]["created_at"],
                include_rows=include_rows,
            )
        )
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
