"""Durable tracking state; callers serialize mutations in the controller."""

from __future__ import annotations

from datetime import datetime
import json
import re

from sqlalchemy import func, or_, select

from core.web_push import NATIVE_PUSH_RELAY_ORIGIN
from storage.models import live_activity_trackings as tracks, session_turns, vault_requests
from storage import web_push_service

PHASE_MAP = {"completed": "done", "failed": "failed", "not_written": "failed", "canceled": "stopped"}
STOP_REASONS = {
    "user_stopped", "dismissed", "system_ended", "logout", "account_switch", "instance_switch", "disabled", "replaced"
}
ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
TOKEN_RE = re.compile(r"(?:[0-9a-f]{2}){8,256}\Z")
REGISTRATION_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def unix(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def get(conn, activity_id: str):
    row = conn.execute(select(tracks).where(tracks.c.activity_id == activity_id)).mappings().first()
    return dict(row) if row else None


def activity(row):
    result = {
        "activity_id": row["activity_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "phase": row["last_phase"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "confirmed_at": row["confirmed_at"],
        "status": row["status"],
    }
    if row["pending_state"]:
        pending = json.loads(row["pending_state"])
        result.update({key: pending[key] for key in ("phase", "started_at", "ended_at")})
    return result


def subscription(conn, owner: str, registration: str):
    return web_push_service.get_enabled_by_endpoint(
        conn,
        user_key=owner,
        endpoint=f"{NATIVE_PUSH_RELAY_ORIGIN}/api/push/apns/{registration}",
    )


def snapshot(conn, session_id: str, now: int, pending_question: bool, turn_id: str | None = None):
    query = select(session_turns).where(session_turns.c.session_id == session_id)
    query = query.where(session_turns.c.id == turn_id) if turn_id else query.where(session_turns.c.state == "active")
    turn = conn.execute(query).mappings().first()
    if turn is None:
        return None
    terminal = turn["state"] == "terminal"
    if not terminal and turn["state"] != "active":
        return None
    pending_vault = (
        conn.execute(
            select(vault_requests.c.id)
            .where(
                vault_requests.c.status == "pending",
                or_(
                    func.json_extract(vault_requests.c.requester, "$.session_id") == session_id,
                    func.json_extract(vault_requests.c.delivery, "$.session_id") == session_id,
                ),
            )
            .limit(1)
        ).first()
        is not None
    )
    started = unix(turn["started_at"] or turn["created_at"])
    return {
        "turn_id": turn["id"],
        "phase": PHASE_MAP[turn["terminal_outcome"]]
        if terminal
        else ("needsYou" if pending_question or pending_vault else "working"),
        "started_at": started,
        "ended_at": max(started, unix(turn["terminal_at"])) if terminal else None,
        "confirmed_at": max(started, now),
    }


def update(conn, activity_id: str, **values):
    conn.execute(tracks.update().where(tracks.c.activity_id == activity_id).values(**values))


def stop(conn, activity_id: str, reason: str, now: int):
    result = conn.execute(
        tracks.update()
        .where(
            tracks.c.activity_id == activity_id,
            tracks.c.status == "tracking",
        )
        .values(status="stopped", stop_reason=reason, confirmed_at=now, pending_state=None, updated_at=now)
    )
    return bool(result.rowcount)


def response(code=200, **body):
    return {"status_code": code, "body": {"ok": code < 400, **body}}


def handle(conn, operation: str, owner: str, activity_id: str, payload: dict, now: int, pending_question: bool):
    if operation == "turn":
        state = snapshot(conn, payload["session_id"], now, pending_question)
        if state is None:
            return response(409, error="no_active_turn")
        return response(session_id=payload["session_id"], turn={k: v for k, v in state.items() if k != "ended_at"})
    if not isinstance(activity_id, str) or not ID_RE.fullmatch(activity_id):
        return response(400, error="invalid_request")
    row = get(conn, activity_id)
    if operation == "delete":
        if (
            set(payload) != {"reason"}
            or not isinstance(payload.get("reason"), str)
            or payload["reason"] not in STOP_REASONS
        ):
            return response(400, error="invalid_request")
        stopped = bool(row and row["owner_user_key"] == owner and stop(conn, activity_id, payload["reason"], now))
        return response(stopped=stopped)
    if operation == "get":
        if row is None or row["owner_user_key"] != owner:
            return response(404, error="not_found")
        result = activity(row)
        if row["status"] == "tracking":
            if now >= row["expires_at"]:
                stop(conn, activity_id, "expired", now)
                result = activity(get(conn, activity_id))
            elif result["ended_at"] is None:
                state = snapshot(conn, row["session_id"], now, pending_question, row["turn_id"])
                if state:
                    result.update({key: state[key] for key in ("phase", "started_at", "ended_at", "confirmed_at")})

        result["confirmed_at"] = max(result["confirmed_at"], now)
        return response(activity=result)
    if operation != "put" or set(payload) != {"session_id", "turn_id", "push_registration_id", "activity_token"}:
        return response(400, error="invalid_request")
    if not all(isinstance(v, str) and 0 < len(v) <= 512 for v in payload.values()) or not (
        TOKEN_RE.fullmatch(payload["activity_token"]) and REGISTRATION_RE.fullmatch(payload["push_registration_id"])
    ):
        return response(400, error="invalid_request")
    if subscription(conn, owner, payload["push_registration_id"]) is None:
        return response(403, error="registration_not_owned")
    if row and (
        row["owner_user_key"] != owner
        or any(
            row[key] != payload[key]
            for key in (
                "session_id",
                "turn_id",
                "push_registration_id",
            )
        )
    ):
        return response(409, error="activity_conflict")
    if row and row["status"] == "tracking" and now >= row["expires_at"]:
        stop(conn, activity_id, "expired", now)
        row = get(conn, activity_id)
    if row and row["status"] != "tracking":
        if row["status"] == "ended":
            return response(409, error="turn_not_active", activity=activity(row))
        return response(activity=activity(row))
    state = snapshot(conn, payload["session_id"], now, pending_question, payload["turn_id"])
    if state is None:
        return response(409, error="turn_not_active")
    terminal = state["ended_at"] is not None
    if row:
        # Rotation never acknowledges an unsent phase, consumes a heartbeat, or extends the lifetime.
        update(conn, activity_id, activity_token=payload["activity_token"], updated_at=now)
    else:
        conn.execute(
            tracks.update()
            .where(
                tracks.c.push_registration_id == payload["push_registration_id"],
                tracks.c.status == "tracking",
            )
            .values(status="stopped", stop_reason="replaced", confirmed_at=now, pending_state=None, updated_at=now)
        )
        conn.execute(
            tracks.insert().values(
                activity_id=activity_id,
                owner_user_key=owner,
                **payload,
                status="ended" if terminal else "tracking",
                last_phase=state["phase"],
                started_at=state["started_at"],
                ended_at=state["ended_at"],
                last_timestamp=now,
                confirmed_at=state["confirmed_at"],
                created_at=now,
                updated_at=now,
                expires_at=now + 28800,
            )
        )
    result = activity(get(conn, activity_id))
    # The caller applies current state locally. Existing rows retain publisher delivery state.
    result.update({key: state[key] for key in ("phase", "started_at", "ended_at", "confirmed_at")})
    if terminal:
        result["status"] = "ended"
        return response(409, error="turn_not_active", activity=result)
    return response(activity=result)
