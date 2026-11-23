"""Process-local admission for measured long creates. Call off-loop; no lock spans a model request."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field

from config.paths import get_state_dir

DAY = 86400
MAX_ROUTES = 4096


@dataclass(frozen=True)
class Trial:
    ticket: str
    reads: int
    proven: bool


@dataclass
class State:
    day: int
    used: int = 0
    # route -> (status, ticket, expiry). A proof is local and has no time expiry.
    routes: dict[bytes, tuple[str, str, float]] = field(default_factory=dict)
    last_loss: bytes = b""
    loss_until: float = 0
    paused_until: float = 0

    def room(self, route: bytes, now: float, preview: bool = False) -> bool:
        if route in self.routes or len(self.routes) < MAX_ROUTES:
            return True
        for key, (status, _, until) in self.routes.items():
            if status == "bootstrap_proven" or until <= now:
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
            row = db.execute("SELECT day, used, loss, until FROM hint WHERE id=1").fetchone()
        if row:
            day, used, loss, until = row
            if type(day) is int and day == state.day and type(used) is int and used >= 0:
                state.used = used
            if (
                isinstance(loss, bytes)
                and len(loss) == 32
                and isinstance(until, (float, int))
                and now < until <= now + DAY
            ):
                state.last_loss, state.loss_until = loss, until
                state.routes[loss] = ("bootstrap_read_loss", "", until)
    except (sqlite3.Error, OSError, ValueError):
        pass  # Snapshot absent, corrupt, or busy. This process still has its own hard limit.
    return state


def _save(state: State) -> None:
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS hint (id INTEGER PRIMARY KEY, day INTEGER, used INTEGER, loss BLOB, until REAL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO hint VALUES (1,?,?,?,?)",
                (state.day, state.used, state.last_loss, state.loss_until),
            )
    except (sqlite3.Error, OSError, ValueError, OverflowError):
        pass  # Never roll back local evidence or a slot, and never retry a cross-process lock.


def _limit() -> int:
    try:
        return max(0, int(os.environ.get("AVIBE_MESSAGE_THREAD_BOOTSTRAP_DAILY_LIMIT", "5")))
    except ValueError:
        return 0


def reserve(route: bytes, reads: int, preview: bool) -> tuple[Trial | None, str]:
    global _state
    with _lock:
        now = time.time()
        state = _state or _load(now)
        if not preview:
            _state = state
        if state.paused_until > now:
            return None, "bootstrap_capacity"
        status, _, until = state.routes.get(route, ("", "", 0))
        proven = status == "bootstrap_proven"
        if not proven and until > now:
            return None, status
        day = max(state.day, int(now // DAY))
        used = state.used if day == state.day else 0
        if not proven and used >= _limit():
            return None, "bootstrap_daily_limit"
        if not state.room(route, now, preview):
            return None, "bootstrap_capacity"
        trial = Trial("" if preview else uuid.uuid4().hex, reads, proven)
        if not preview:
            state.day, state.used = day, used + int(not proven)
            state.routes[route] = ("bootstrap_pending", trial.ticket, now + DAY)
            if not proven:
                _save(state)
        return trial, "bootstrap_recheck" if proven else "bootstrap_trial"


def _owns(route: bytes, trial: Trial, now: float) -> bool:
    if _state is None or _state.paused_until > now:
        return False
    status, ticket, until = _state.routes.get(route, ("", "", 0))
    return status == "bootstrap_pending" and ticket == trial.ticket and until > now


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
                state.routes[route] = (status, "", now + DAY)
            else:
                # Late loss after eviction with all slots occupied. Never evict another live cooldown.
                state.paused_until = now + DAY
            _save(state)
        elif _owns(route, trial, now):
            state.routes[route] = (status, "", now + DAY)
        else:
            return "bootstrap_stale"
        return status
