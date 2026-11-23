"""Instance-local admission ledger for measured, risky Anthropic creates. Call only off the controller loop."""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass

from config.paths import get_state_dir

DAY = 86400
MAX_ROUTES = 4096


@dataclass(frozen=True)
class Trial:
    ticket: str
    reads: int
    proven: bool


def _limit() -> int:
    try:
        return max(0, int(os.environ.get("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "5")))
    except ValueError:
        return 0


def reserve(route: bytes, reads: int, preview: bool) -> tuple[Trial | None, str]:
    """Atomic precharge (never refunded) and route lease. A peek cannot create even a file."""
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    try:
        if preview and not path.exists():
            return (Trial("", reads, False), "bootstrap_trial") if _limit() else (None, "bootstrap_daily_limit")
        if not preview:
            path.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(
                sqlite3.connect(path.as_uri() + ("?mode=ro" if preview else "?mode=rwc"), uri=True, timeout=0.2)
            ) as db,
            db,
        ):
            db.execute("BEGIN" if preview else "BEGIN IMMEDIATE")
            if not preview:
                db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, day INTEGER, used INTEGER)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS routes (route BLOB PRIMARY KEY, ticket TEXT, status TEXT, until REAL)"
                )
            now = time.time()
            day = int(now // DAY)
            row = db.execute("SELECT status,until FROM routes WHERE route=?", (route,)).fetchone()
            proven = bool(row and row[0] == "bootstrap_proven")
            if row and not proven and row[1] > now:
                return None, row[0]
            budget = db.execute("SELECT day,used FROM budget WHERE id=1").fetchone()
            used = budget[1] if budget and day <= budget[0] else 0
            if not proven and used >= _limit():
                return None, "bootstrap_daily_limit"
            if row is None and db.execute("SELECT count(*) FROM routes").fetchone()[0] >= MAX_ROUTES:
                # Never evict a live block to make room. Forgetting a proof only costs another trial.
                evict = db.execute(
                    "SELECT route FROM routes WHERE until<=? OR status='bootstrap_proven' LIMIT 1", (now,)
                ).fetchone()
                if evict is None:
                    return None, "bootstrap_capacity"
                if not preview:
                    db.execute("DELETE FROM routes WHERE route=?", evict)
            ticket = "" if preview else uuid.uuid4().hex
            if not preview:
                if not proven:
                    db.execute(
                        "INSERT OR REPLACE INTO budget VALUES (1,?,?)",
                        (max(day, budget[0]) if budget else day, used + 1),
                    )
                # An interrupted/unmeasured trial fails closed, even across a process restart.
                db.execute(
                    "INSERT OR REPLACE INTO routes VALUES (?,?,?,?)", (route, ticket, "bootstrap_pending", now + DAY)
                )
            return Trial(ticket, reads, proven), "bootstrap_recheck" if proven else "bootstrap_trial"
    except (sqlite3.Error, OSError, ValueError):
        return None, "bootstrap_state_unavailable"


def settle(route: bytes, trial: Trial, status: str) -> str:
    """Only the owning generation can prove a route. A lost read always wins over overlapping success."""
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.2)) as db, db:
            now = time.time()
            if status == "bootstrap_read_loss":
                db.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?)", (route, "", status, now + DAY))
            else:
                # Keep the precharged lease until proof, or until an abandoned trial expires.
                changed = db.execute(
                    "UPDATE routes SET status=?,until=? WHERE route=? AND ticket=? AND status='bootstrap_pending' AND until>?",
                    (status, now + DAY, route, trial.ticket, now),
                )
                if not changed.rowcount:
                    return "bootstrap_stale"
        return status
    except (sqlite3.Error, OSError, ValueError):
        return "bootstrap_state_unavailable"
