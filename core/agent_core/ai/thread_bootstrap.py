"""Process-local admission for measured long creates, with best-effort restart hints."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from core import anthropic_thread_engine as engine
from core.agent_core.ai import thread_bootstrap_hint as hint

DAY = hint.DAY
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
    fenced: bool = False


@dataclass(frozen=True)
class Entry:
    status: str
    until: float = 0
    trial: Trial | None = None
    version: str | None = None
    # Published or restored: this exact generation must be consumed by a committed write.
    generation: str | None = None

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


def _restore(snap: hint.Snapshot) -> State:
    state = State(snap.day, snap.used, last_loss=snap.loss, loss_until=snap.until)
    if snap.loss:
        state.routes[snap.loss] = Entry("bootstrap_read_loss", snap.until)
    for route, (version, generation) in snap.proofs.items():
        state.routes[route] = Entry("bootstrap_proven", version=version, generation=generation)
    return state


_state: State | None = None
_lock = threading.Lock()


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
        snapshot = hint.load(time.time, MAX_ROUTES) if preview or _state is None else None
        state = _state if _state is not None else _restore(snapshot)
        now = time.time()
        if not preview:
            _state = state
        if state.paused_until > now:
            return None, "bootstrap_capacity"
        version = engine.version(version)
        entry = state.routes.get(route)
        if entry and entry.blocks(now):
            return None, entry.status
        candidate = entry if entry and entry.status == "bootstrap_proven" and entry.version == version else None
        expected = candidate.generation if candidate else None
        live_only = candidate is not None and expected is None
        day = max(state.day, int(now // DAY))
        used = state.used if day == state.day else 0
        can_charge = used < _limit()
        if not candidate and not can_charge:
            return None, "bootstrap_daily_limit"
        if not state.room(route, now, preview=True):
            return None, "bootstrap_capacity"
        proven, fenced = live_only, False
        ticket = "" if preview else uuid.uuid4().hex
        if preview:
            if snapshot.loss == route:
                return None, "bootstrap_read_loss"
            if expected is not None:
                proven = snapshot.proofs.get(route) == (version, expected)
        else:
            try:
                with hint.transaction(time.time, MAX_ROUTES) as (db, snap):
                    if snap.loss == route:
                        state.room(route, now)
                        state.routes[route] = Entry("bootstrap_read_loss", snap.until)
                        if snap.until >= state.loss_until:
                            state.last_loss, state.loss_until = route, snap.until
                        return None, "bootstrap_read_loss"
                    day = max(state.day, snap.day)
                    used = state.used if day == state.day else 0
                    can_charge = used < _limit()
                    consumed, fenced = hint.reserve(db, route, version, ticket, expected, can_charge, MAX_ROUTES)
                    proven = live_only or consumed
                    if proven or can_charge:
                        hint.write_hint(db, state, snap, day, used + int(not proven))
            except hint.ERRORS:
                # Includes failure at COMMIT after the conditional UPDATE matched. No retry and no durable exemption.
                if entry := state.routes.get(route):
                    if entry.status == "bootstrap_read_loss" and entry.until > now:
                        return None, entry.status
                proven, fenced = live_only, False
        if not proven and not can_charge:
            return None, "bootstrap_daily_limit"
        trial = Trial(ticket, reads, proven, version, lease, fenced)
        if not preview:
            state.room(route, now)
            state.day, state.used = day, used + int(not proven)
            state.routes[route] = Entry("bootstrap_pending", now + DAY, trial)
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
            try:
                with hint.transaction(time.time, MAX_ROUTES) as (db, snap):
                    db.execute("DELETE FROM proof WHERE route=?", (route,))  # Revoke either proof or publication fence.
                    hint.write_hint(db, state, snap, state.day, state.used)
            except hint.ERRORS:
                pass  # Never roll back live measured loss.
        elif not _owns(route, trial, now):
            return "bootstrap_stale"
        elif status == "bootstrap_proven":
            generation = None
            if trial.fenced:
                try:
                    with hint.transaction(time.time, MAX_ROUTES) as (db, _):
                        if not hint.publish(db, route, trial):
                            del state.routes[route]
                            return "bootstrap_stale"
                except hint.ERRORS:
                    # Publication is uncertain. Require the durable CAS rather than mint a second local capability.
                    pass
                generation = trial.ticket
            state.routes[route] = Entry(status, version=trial.version, generation=generation)
        else:
            state.routes[route] = Entry(status, now + DAY)
        return status
