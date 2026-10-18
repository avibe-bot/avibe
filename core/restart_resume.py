"""How startup recovery settles a Vibey Turn an Avibe restart cut.

The Turn reruns in place (``docs/plans/vibey-restart-auto-resume.md``): it keeps its
identity, accepted start, Deliveries, and Runs. This module only decides; the Turn
owner (``core.session_turns``) writes. Every read is bounded by the Turn or by the
Session's context tail, since recovery runs it on the controller loop.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.engine import Connection

from storage import agent_transcript, message_deliveries as deliveries
from storage.background import normalize_run_status
from storage.models import agent_runs, agent_sessions

#: The restart that would be the third in a row to cut a Turn with no committed model
#: response in between falls back to the restart notice.
RESTART_RESUME_LIMIT = 3
#: This process: a restart is counted once, however often its recovery retries.
PROCESS_TOKEN = secrets.token_hex(8)
_PENDING_CONTROL_STATES = frozenset({"pending", "interrupting", "waiting_terminal", "reconciling"})

Plan = Literal["wait", "rerun", "unavailable"]


def restart_streak(conn: Connection, turn: dict[str, Any]) -> tuple[int, int | None]:
    """The restarts in a row that have cut ``turn``, this one included, and the response mark.

    The mark is the context sequence of the Session's last committed model response.
    A response committed since the previous restart resets the streak to this one.
    """
    previous = deliveries.restart_receipt(turn)
    streak = int(previous.get("restart_streak") or 0)
    if streak and previous.get("restart_process") == PROCESS_TOKEN:
        # This process already counted this restart (its recovery is being retried).
        return streak, previous.get("restart_response_seq")
    responded = agent_transcript.last_response(conn, str(turn["session_id"]))
    response_seq = responded.context_seq if responded is not None else None
    if streak and previous.get("restart_response_seq") != response_seq:
        streak = 0
    return streak + 1, response_seq


def _runs_released(conn: Connection, turn_id: str) -> bool:
    """Whether a Run of the Turn asked to stop, or a Run it started for has ended.

    The runner refuses to dispatch a Turn whose initial Run already ended
    (``_turn_has_terminal_run``), so such a Turn keeps the notice instead.
    """
    run_ids = deliveries.accepted_agent_run_ids_for_turn(conn, turn_id)
    if not run_ids:
        return False
    initial_runs = {
        run_id
        for delivery in deliveries.initial_deliveries_for_turn(conn, turn_id)
        for run_id in deliveries.agent_run_ids_for_delivery(conn, delivery)
    }
    rows = conn.execute(
        select(agent_runs.c.id, agent_runs.c.status, agent_runs.c.cancel_requested).where(
            agent_runs.c.id.in_(run_ids)
        )
    ).mappings()
    for row in rows:
        live = normalize_run_status(row["status"]) in {"queued", "running"}
        if (live and bool(row["cancel_requested"])) or (not live and str(row["id"]) in initial_runs):
            return True
    return False


def restart_resume_plan(conn: Connection, turn: dict[str, Any]) -> Plan:
    """How recovery settles an accepted Vibey Turn a restart cut.

    ``"wait"``: a steer into it is still unsettled (its receipt could not be
    materialized yet); the Turn stays open and recovery retries, so the steer joins
    the Turn before it reruns.
    ``"rerun"``: the Turn reruns in place from its committed transcript. One whose
    final response was committed ends there, as its live run would have.
    ``"unavailable"``: it keeps the restart notice (start not accepted, Stop or
    replacement requested, a Run cancelled or already ended, Session archived or
    system, Agent off Vibey, or the loop bound reached).
    """
    if (
        turn.get("backend") != "vibey"
        or turn.get("state") != "active"
        or turn.get("start_receipt_outcome") != "accepted"
    ):
        return "unavailable"
    if deliveries.turn_has_unresolved_steer(conn, str(turn["id"])):
        return "wait"
    session = conn.execute(
        select(agent_sessions.c.status, agent_sessions.c.visibility, agent_sessions.c.agent_backend).where(
            agent_sessions.c.id == turn["session_id"]
        )
    ).mappings().one_or_none()
    if (
        session is None
        or session["status"] != "active"
        or session["visibility"] == "system"
        or session["agent_backend"] != "vibey"
        or turn.get("control_mode")
        or turn.get("control_state") in _PENDING_CONTROL_STATES
        or _runs_released(conn, str(turn["id"]))
        or restart_streak(conn, turn)[0] >= RESTART_RESUME_LIMIT
    ):
        return "unavailable"
    return "rerun"
