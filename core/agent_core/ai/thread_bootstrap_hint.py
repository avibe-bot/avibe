"""Zero-wait SQLite hint. A published proof is a single-use generation, not a cached permission."""

from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field

from config.paths import get_state_dir
from core import anthropic_thread_engine as engine

DAY = 86400
ERRORS = (sqlite3.Error, OSError, ValueError, OverflowError)


@dataclass
class Snapshot:
    observed_at: float
    used: int = 0
    loss: bytes = b""
    until: float = 0
    proofs: dict[bytes, tuple[str, str]] = field(default_factory=dict)
    valid: bool = False
    # Valid positives outside the restore bound (oldest first to forget); not malformed evidence.
    excess: tuple[bytes, ...] = ()

    @property
    def day(self) -> int:
        return int(self.observed_at // DAY)


def _ready_slots(capacity, active_loss):
    return max(0, capacity - int(bool(active_loss)))


def _snapshot(db, clock, capacity):
    rows = db.execute("SELECT id, day, used, loss, until FROM hint LIMIT 2").fetchall()
    # BEGIN (without IMMEDIATE) does not establish a read snapshot. A concurrent writer can
    # commit a newer loss right up to this SELECT. Validate the observed rows, not an earlier clock.
    now = clock()
    snap = Snapshot(now)
    if len(rows) != 1 or rows[0][0] != 1:
        return snap
    _, day, used, loss, until = rows[0]
    count_ok = type(day) is int and 0 <= day <= snap.day and type(used) is int and used >= 0
    if count_ok and day == snap.day:
        snap.used = used
    loss_ok = (
        isinstance(loss, bytes)
        and type(until) in (int, float)
        and math.isfinite(until)
        and ((not loss and until == 0) or (len(loss) == 32 and 0 < until <= now + DAY))
    )
    if loss_ok and until > now:
        snap.loss, snap.until = loss, until
    if not (count_ok and loss_ok):
        return snap
    # Older controllers only have the count/loss table.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='proof'").fetchone():
        rows = db.execute(
            "SELECT route, version, generation, ready FROM proof ORDER BY rowid DESC LIMIT ?", (capacity + 1,)
        ).fetchall()
        seen, proofs = set(), {}
        for route, version, generation, ready in rows:
            if (
                not isinstance(route, bytes)
                or len(route) != 32
                or route in seen
                or route == snap.loss
                or not engine.version(version)
                or not isinstance(generation, str)
                or not re.fullmatch(r"[0-9a-f]{32}", generation)
                or type(ready) is not int
                or ready not in (0, 1)
            ):
                return snap
            seen.add(route)
            if ready:
                proofs[route] = (version, generation)
        if len(seen) > capacity:
            return snap
        snap.excess = tuple(proofs)[_ready_slots(capacity, snap.loss) :]
        for route in snap.excess:
            del proofs[route]
        # In-memory room() forgets the first optional entry: keep oldest first there too.
        snap.proofs = dict(reversed(proofs.items()))
    snap.valid = True
    return snap


def load(clock, capacity):
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("BEGIN")
            return _snapshot(db, clock, capacity)
    except ERRORS:
        return Snapshot(clock())


@contextmanager
def transaction(clock, capacity):
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=0)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS hint (id INTEGER PRIMARY KEY, day INTEGER, used INTEGER, loss BLOB, until REAL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS proof (route BLOB PRIMARY KEY, version TEXT NOT NULL, generation TEXT NOT NULL, ready INTEGER NOT NULL)"
        )
        snap = _snapshot(db, clock, capacity)
        if not snap.valid:
            # Repair and invalidation must commit together. A later count write must not legitimize rejected rows.
            db.execute("DELETE FROM proof")
            db.execute("DELETE FROM hint")
            # Preserve independently valid negative evidence even when the caller denies admission.
            db.execute("INSERT INTO hint VALUES (1,?,?,?,?)", (snap.day, snap.used, snap.loss, snap.until))
        elif snap.excess:
            db.executemany("DELETE FROM proof WHERE route=? AND ready=1", ((route,) for route in snap.excess))
        yield db, snap
        # Promotion and revocation can both reduce available room. Preserve the cooldown and all
        # still-needed publication fences; expire only excess ready generations before COMMIT.
        row = db.execute("SELECT until FROM hint WHERE id=1").fetchone()
        slots = _ready_slots(capacity, row and row[0] > snap.observed_at)
        db.execute(
            "DELETE FROM proof WHERE rowid IN (SELECT rowid FROM proof WHERE ready=1 ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (slots,),
        )


def write_hint(db, state, snap, day, used):
    loss, until = (state.last_loss, state.loss_until) if state.loss_until >= snap.until else (snap.loss, snap.until)
    db.execute("INSERT OR REPLACE INTO hint VALUES (1,?,?,?,?)", (day, used, loss, until))
    if until > snap.observed_at:
        db.execute("DELETE FROM proof WHERE route=?", (loss,))


def reserve(db, route, version, ticket, expected, can_charge, capacity):
    """Return (consumed exact proof, installed publication fence). No result authorizes a send until commit."""
    if expected is not None:
        consumed = (
            db.execute(
                "UPDATE proof SET generation=?, ready=0 WHERE route=? AND version=? AND generation=? AND ready=1",
                (ticket, route, version, expected),
            ).rowcount
            == 1
        )
        if consumed or not can_charge:
            return consumed, consumed
    if version is None:
        db.execute("DELETE FROM proof WHERE route=?", (route,))
        return False, False
    # On a stale proof miss, a charged trial must not take a newer generation away from its owner.
    insert = "INSERT OR IGNORE" if expected else "INSERT OR REPLACE"
    fenced = db.execute(f"{insert} INTO proof VALUES (?,?,?,0)", (route, version, ticket)).rowcount == 1
    db.execute(
        "DELETE FROM proof WHERE rowid IN (SELECT rowid FROM proof ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (capacity,)
    )
    return False, fenced


def publish(db, route, trial):
    return (
        db.execute(
            "UPDATE proof SET ready=1 WHERE route=? AND version=? AND generation=? AND ready=0",
            (route, trial.version, trial.ticket),
        ).rowcount
        == 1
    )
