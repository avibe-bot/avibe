"""Instance-local admission ledger for measured, risky Anthropic creates. Call only off the controller loop."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from dataclasses import dataclass

from config.paths import get_state_dir
from storage.lock import MigrationFileLock

DAY = 86400
MAX_ROUTES = 4096


@dataclass(frozen=True)
class Trial:
    ticket: str
    reads: int
    proven: bool


def _loss_marker(route: bytes):
    return get_state_dir() / "message-thread-loss" / route.hex()


def _regular_stamp(path) -> float:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    return info.st_mtime if stat.S_ISREG(info.st_mode) else 0


def _directory(path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _lost_at(route: bytes) -> float:
    marker = _loss_marker(route)
    if not _directory(marker.parent):
        return 0
    # Order matters: GC can move active -> retiring, never the reverse. Read both even after restart.
    active = _regular_stamp(marker)
    retiring = marker.parent.with_suffix(".retiring")
    return max(active, _regular_stamp(retiring / marker.name) if _directory(retiring) else 0)


def _write_loss(route: bytes) -> str:
    # Never wait for the sweeper, SQLite, or a suspended writer to preserve measured loss.
    marker = _loss_marker(route)
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not _directory(marker.parent):
        raise OSError("Invalid receipt directory")
    for _ in range(3):
        try:
            mode = marker.lstat().st_mode
        except FileNotFoundError:
            try:
                marker.touch(exist_ok=False)
                return "bootstrap_read_loss"
            except FileExistsError:
                continue  # Another renewal created it first.
        if not stat.S_ISREG(mode):
            raise OSError("Invalid loss receipt")
        try:
            os.utime(marker, follow_symlinks=False)
            return "bootstrap_read_loss"
        except FileNotFoundError:
            continue  # GC just retired that generation; create the fresh active one.
    raise OSError("Could not persist loss receipt")


def _expired(directory, cutoff):
    if _directory(directory):
        for p in directory.iterdir():
            if re.fullmatch("[0-9a-f]{64}", p.name) and (stamp := _regular_stamp(p)) and stamp <= cutoff:
                yield p, stamp


def _prune_losses(db, directory) -> None:
    if not _directory(directory):
        return
    # Only maintenance ever takes this try-lock, and only before starting a SQLite transaction.
    # Busy => fail closed for admission/proof; the loss path can still write its active receipt.
    with MigrationFileLock(directory.with_suffix(".lock"), timeout_seconds=0):
        cutoff = time.time() - DAY
        retiring = directory.with_suffix(".retiring")
        if os.path.lexists(retiring) and not _directory(retiring):
            return  # Never claim a foreign path for housekeeping.
        for marker, _ in _expired(directory, cutoff):
            retiring.mkdir(exist_ok=True)
            target = retiring / marker.name
            if not os.path.lexists(target):
                marker.rename(target)
        # Recheck *after* the atomic handoff: a concurrent renewal before rename moves with it;
        # after rename it writes the active name and GC cannot unlink it. Persist revocations first.
        expired = list(_expired(retiring, cutoff))
        if expired:
            with db:
                db.executemany(
                    "DELETE FROM routes WHERE route=? AND status='bootstrap_proven' AND until<=?",
                    [(bytes.fromhex(p.name), stamp + DAY) for p, stamp in expired],
                )
            for marker, _ in expired:
                marker.unlink()


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
            if (lost := _lost_at(route)) and lost + DAY > time.time():
                return None, "bootstrap_read_loss"
            return (Trial("", reads, False), "bootstrap_trial") if _limit() else (None, "bootstrap_daily_limit")
        if not preview:
            path.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(
                sqlite3.connect(path.as_uri() + ("?mode=ro" if preview else "?mode=rwc"), uri=True, timeout=0.2)
            ) as db,
            db,
        ):
            if not preview:
                db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, day INTEGER, used INTEGER)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS routes (route BLOB PRIMARY KEY, ticket TEXT, status TEXT, until REAL)"
                )
                _prune_losses(db, _loss_marker(route).parent)
            db.execute("BEGIN" if preview else "BEGIN IMMEDIATE")
            now = time.time()
            day = int(now // DAY)
            row = db.execute("SELECT status,until FROM routes WHERE route=?", (route,)).fetchone()
            lost = _lost_at(route)
            if lost and lost + DAY > now:
                return None, "bootstrap_read_loss"
            proven = bool(row and row[0] == "bootstrap_proven" and row[1] - DAY > lost)
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


def owns(route: bytes, trial: Trial) -> bool:
    """A local parent is single-use, but is not authority to spend an expired or superseded reservation."""
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            now = time.time()
            return (
                not ((lost := _lost_at(route)) and lost + DAY > now)
                and db.execute(
                    "SELECT 1 FROM routes WHERE route=? AND ticket=? AND status='bootstrap_pending' AND until>?",
                    (route, trial.ticket, now),
                ).fetchone()
                is not None
            )
    except (sqlite3.Error, OSError, ValueError):
        return False


def settle(route: bytes, trial: Trial, status: str) -> str:
    """Only the owning generation can prove a route. A lost read always wins over overlapping success."""
    path = get_state_dir() / "message-thread-bootstrap.sqlite"
    orphaned = False
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.2)) as db, db:
            _prune_losses(db, _loss_marker(route).parent)
            now = time.time()
            if status == "bootstrap_read_loss":
                # Never resurrect an evicted reservation at the expense of a different live cooldown.
                orphaned = not db.execute(
                    "UPDATE routes SET ticket='',status=?,until=? WHERE route=?", (status, now + DAY, route)
                ).rowcount
            else:
                db.execute("BEGIN IMMEDIATE")
                if (lost := _lost_at(route)) and lost + DAY > now:
                    return "bootstrap_stale"
                # Keep the precharged lease until proof, or until an abandoned trial expires.
                changed = db.execute(
                    "UPDATE routes SET status=?,until=? WHERE route=? AND ticket=? AND status='bootstrap_pending' AND until>?",
                    (status, now + DAY, route, trial.ticket, now),
                )
                if not changed.rowcount:
                    return "bootstrap_stale"
        return _write_loss(route) if orphaned else status
    except (sqlite3.Error, OSError, ValueError):
        # A contended ledger must not shorten a measured loss to 24h from the earlier reservation.
        # Independent of SQLite's write lock; a later process checks this receipt before admission/proof.
        if status == "bootstrap_read_loss":
            try:
                return _write_loss(route)
            except OSError:
                pass
        return "bootstrap_state_unavailable"
