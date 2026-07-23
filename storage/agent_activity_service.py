"""Turn-grouped agent activity for the Web Chat Activity panel.

Composes the two persisted trace sources into per-turn groups:

* interim ``assistant`` messages (``messages`` table, ``type='assistant'``), and
* ``tool_call`` events (``agent_events`` table, ``event_type='tool_call'``).

A *turn* is bounded by transcript markers rather than an id: it ends at the
agent's terminal reply (``result`` / ``error`` / backend-failure ``notify``) or,
when the user starts a new turn without one, is reported as ``interrupted``.
Grouping is chronological because ``messages`` carries no ``turn_id`` (only
``agent_events`` does). Both tables persist WHOLE-SECOND ``...Z`` ``created_at``,
which cannot order same-second rows — but both also mint ids with a MICROSECOND
clock prefix (``<pfx>_<15-hex microsecond epoch><uuid8>``), so the merge sorts by
that decoded microsecond, recovering the true emission order ACROSS tables (a fast
turn's tool call before its same-second terminal; one turn's terminal before the
next turn's same-second opener). A phase tiebreak (turn-start < activity <
terminal) only applies when the microsecond can't be decoded (format drift), and
the whole-second ``created_at`` still bounds the event scan.

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
# terminals (result/error/notify/silent-marker), and the interim assistant activity
# rows. The invisible ``silent`` marker is fetched here (it is NOT in TRANSCRIPT_TYPES)
# so a turn that completed with no user-visible reply still has a terminal to close on.
_RELEVANT_MESSAGE_TYPES = (
    "user",
    messages_service.HARNESS_TYPE,
    "result",
    "error",
    "notify",
    messages_service.SILENT_TYPE,
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
    """Whether an agent message legally CLOSES a turn.

    Terminals: a visible ``result``/``error`` reply, a ``backend_failure`` ``notify``
    diagnostic, OR — when the turn produced nothing user-visible (a ``<silent>``-
    stripped/empty final, or a reply-less bookkeeping turn) — the invisible ``silent``
    marker persisted at the delivery chokepoint. Only cancel/Stop (no terminal at all)
    stays ``interrupted``.

    A PLAIN ``notify`` is deliberately NOT terminal: agents emit mid-turn notify rows
    that explicitly do not end the turn (e.g. Claude's model-refusal fallback), so
    treating every notify as terminal would split one turn into two groups. A genuine
    notify-only COMPLETION is instead closed by the ``silent`` marker (its turn still
    emits an empty final result at the chokepoint), not by the notify row.
    """
    if author != "agent":
        return False
    if msg_type in ("result", "error", messages_service.SILENT_TYPE):
        return True
    if msg_type == "notify" and (metadata or {}).get("event") == "backend_failure":
        return True
    return False


def _terminal_status(msg_type: Any, metadata: Optional[dict] = None) -> str:
    """done for a normal completion (result / silent marker); failed for an ``error``
    or a ``backend_failure`` notify."""
    if msg_type == "error":
        return "failed"
    if msg_type == "notify" and (metadata or {}).get("event") == "backend_failure":
        return "failed"
    return "done"


# Fallback tiebreak only (used when a row's microsecond id prefix can't be decoded,
# e.g. format drift): within a single turn the order is open → work → close.
_PHASE_RANK = {"turn_start": 0, "activity": 1, "terminal": 2, "ignore": 3}


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
        # Show-Page annotations/intents persist as transcript rows (``user`` AND
        # ``assistant``) with metadata.source='show_page'; they are display-only and
        # must never act as a turn opener (would split an in-flight turn) nor as
        # activity — always ignore them in the grouping.
        show_page = metadata.get("source") == "show_page"
        if _is_terminal(mtype, author, metadata):
            kind = "terminal"
        elif mtype in ("user", messages_service.HARNESS_TYPE) and not show_page:
            kind = "turn_start"
        elif mtype == "assistant" and not show_page:
            kind = "activity"
        else:
            kind = "ignore"
        mts = _parse_ts(msg.get("created_at"))
        items.append(
            {
                "ts": mts,
                "sort": _emit_micros(msg.get("id"), mts),
                "rank": _PHASE_RANK[kind],
                "created_at": msg.get("created_at"),
                "kind": kind,
                "id": msg.get("id"),
                "mtype": mtype,
                "row_kind": "assistant",
                "text": msg.get("text") if include_text else None,
                # The silent marker is a terminal that is INVISIBLE in the transcript,
                # so a group closing on it must anchor to the (visible) turn trigger
                # rather than the marker itself; ``terminal_status`` is resolved here so
                # ``notify`` failure/normal is decided with its metadata in hand.
                "is_silent": mtype == messages_service.SILENT_TYPE,
                "terminal_status": _terminal_status(mtype, metadata) if kind == "terminal" else None,
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
        event_sort = _emit_micros(event.get("id"), event_ts)
        if oldest_msg_sort is not None and event_sort < oldest_msg_sort:
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
                "row_kind": "tool_call",
                "text": event.get("text") if include_text else None,
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
    # Id of the most recent transcript-visible boundary (turn_start OR terminal). An
    # interrupted turn anchors BACKWARD to this — the boundary immediately before its
    # activity (its trigger) — so its chip is positioned by its OWN chronology and
    # never attaches to a future message (the ordering bug).
    last_boundary_id: Optional[str] = None
    for item in items:
        kind = item["kind"]
        if kind == "activity":
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
                    )
                )
                pending = []
            turn_start_iso = item["created_at"]
            last_boundary_id = item["id"]
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
                    )
                )
                pending = []
            turn_start_iso = None
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
