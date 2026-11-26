"""Process-local admission for measured long creates, with best-effort restart hints."""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field

from core import anthropic_thread_engine as engine
from config.paths import get_state_dir

DAY = 86400
MAX_ROUTES = 4096


class Lease:
    """Who can finish the pair? Its small lock never spans ledger I/O or model work."""

    def __init__(self, live: Callable[[], object]):
        self._live = live
        self._lock = threading.Lock()

    def live(self) -> bool:
        with self._lock:
            return bool(self._live())

    def handoff(self, successor: Callable[[], object]) -> None:
        with self._lock:
            if self._live():
                self._live = successor


@dataclass(frozen=True)
class Trial:
    ticket: str
    reads: int
    proven: bool
    version: str | None = None
    lease: Lease | None = None


@dataclass(frozen=True)
class Entry:
    status: str
    until: float = 0
    trial: Trial | None = None
    version: str | None = None

    def blocks(self, now: float) -> bool:
        if self.trial and self.trial.lease:
            return self.trial.lease.live()
        return self.until > now


@dataclass
class State:
    day: int
    used: int = 0
    routes: dict[bytes, Entry] = field(default_factory=dict)
    last_loss: bytes = b""
    loss_until: float = 0
    paused_until: float = 0

    def room(self, route: bytes, now: float, preview: bool = False) -> bool:
        if route in self.routes or len(self.routes) < MAX_ROUTES:
            return True
        for key, entry in self.routes.items():
            if not entry.blocks(now):
                if not preview:
                    del self.routes[key]
                return True
        return False


_state: State | None = None
_lock = threading.Lock()


def _load(now: float) -> State:
    state = State(int(now // DAY))
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("BEGIN")  # Validate all positive evidence against the very same snapshot.
            rows = db.execute("SELECT id, day, used, loss, until FROM hint LIMIT 2").fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                return state
            _, day, used, loss, until = rows[0]
            valid_count = type(day) is int and 0 <= day <= state.day and type(used) is int and used >= 0
            if valid_count and day == state.day:
                state.used = used
            valid_loss = (
                isinstance(loss, bytes)
                and type(until) in (int, float)
                and math.isfinite(until)
                and ((not loss and until == 0) or (len(loss) == 32 and 0 < until <= now + DAY))
            )
            if valid_loss and until > now:
                state.last_loss, state.loss_until = loss, until
                state.routes[loss] = Entry("bootstrap_read_loss", until)
            if not (valid_count and valid_loss):
                return state
            proofs = db.execute("SELECT route, version FROM proof LIMIT ?", (MAX_ROUTES + 1,)).fetchall()
            if len(proofs) + len(state.routes) > MAX_ROUTES:
                return state
            restored = {}
            for route, version in proofs:
                if (
                    not isinstance(route, bytes)
                    or len(route) != 32
                    or not engine.version(version)
                    or route in state.routes
                    or route in restored
                ):
                    return state
                restored[route] = Entry("bootstrap_proven", version=version)
            state.routes.update(restored)
    except (sqlite3.Error, OSError, ValueError):
        pass  # Includes old count/loss-only databases and busy or partial snapshots.
    return state


def _save(state: State, route: bytes, proof: str | None = None) -> None:
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS hint (id INTEGER PRIMARY KEY, day INTEGER, used INTEGER, loss BLOB, until REAL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS proof (route BLOB PRIMARY KEY, version TEXT NOT NULL)")
            db.execute(
                """INSERT INTO hint VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   day=excluded.day, used=excluded.used,
                   loss=CASE WHEN hint.until > excluded.until THEN hint.loss ELSE excluded.loss END,
                   until=max(hint.until, excluded.until)""",
                (state.day, state.used, state.last_loss, state.loss_until),
            )
            # If a previously busy write now publishes a live cooldown, revoke that proof in this transaction too.
            db.execute("DELETE FROM proof WHERE route=(SELECT loss FROM hint WHERE id=1 AND until>?)", (time.time(),))
            # Event-local mutation, NEVER republish the process's whole map: another controller may have revoked a proof.
            db.execute("DELETE FROM proof WHERE route=?", (route,))
            if proof is not None:
                db.execute(
                    "INSERT INTO proof SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM hint WHERE loss=? AND until>?)",
                    (route, proof, route, time.time()),
                )
                db.execute(
                    "DELETE FROM proof WHERE rowid IN (SELECT rowid FROM proof ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                    (MAX_ROUTES,),
                )
    except (sqlite3.Error, OSError, ValueError, OverflowError):
        pass  # Never roll back local evidence or a slot, and never retry a cross-process lock.


def _limit() -> int:
    try:
        return max(0, int(os.environ.get("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "5")))
    except ValueError:
        return 0


def reserve(
    route: bytes, reads: int, preview: bool, *, version: str | None = None, lease: Lease | None = None
) -> tuple[Trial | None, str]:
    global _state
    with _lock:
        now = time.time()
        state = _state or _load(now)
        if not preview:
            _state = state
        if state.paused_until > now:
            return None, "bootstrap_capacity"
        version = engine.version(version)
        entry = state.routes.get(route)
        proven = bool(entry and entry.status == "bootstrap_proven" and entry.version == version)
        if entry and entry.blocks(now):
            return None, entry.status
        day = max(state.day, int(now // DAY))
        used = state.used if day == state.day else 0
        if not proven and used >= _limit():
            return None, "bootstrap_daily_limit"
        if not state.room(route, now, preview):
            return None, "bootstrap_capacity"
        trial = Trial("" if preview else uuid.uuid4().hex, reads, proven, version, lease)
        if not preview:
            state.day, state.used = day, used + int(not proven)
            state.routes[route] = Entry("bootstrap_pending", now + DAY, trial)
            _save(state, route)
        return trial, "bootstrap_recheck" if proven else "bootstrap_trial"


def _owns(route: bytes, trial: Trial, now: float) -> bool:
    if _state is None or _state.paused_until > now:
        return False
    entry = _state.routes.get(route)
    return bool(entry and entry.trial and entry.trial.ticket == trial.ticket and entry.blocks(now))


def owns(route: bytes, trial: Trial) -> bool:
    with _lock:
        return _owns(route, trial, time.time())


def settle(route: bytes, trial: Trial, status: str) -> str:
    with _lock:
        state, now = _state, time.time()
        if state is None:
            return "bootstrap_stale"
        if status == "bootstrap_read_loss":
            state.last_loss, state.loss_until = route, now + DAY
            if state.room(route, now):
                state.routes[route] = Entry(status, now + DAY)
            else:
                state.paused_until = now + DAY
            _save(state, route)
        elif _owns(route, trial, now):
            if status == "bootstrap_proven":
                state.routes[route] = Entry(status, version=trial.version)
                if trial.version:
                    _save(state, route, trial.version)
            else:
                state.routes[route] = Entry(status, now + DAY)
        else:
            return "bootstrap_stale"
        return status
