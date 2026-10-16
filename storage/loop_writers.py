"""Measure which SQLite write transactions run on the controller loop (#2710).

A write on the controller loop stalls every Session while it waits for SQLite's
writer or for its own commit, and the writers are reached through so many
indirections that no reviewed list of them stays complete. So the engine factory
counts them instead. ``storage.db.create_sqlite_engine`` opens every connection
as a ``CensusConnection`` and listens to the pool, never to the connection: a
SQLAlchemy connection event would put every statement of every thread on the
slower event-dispatch path.

- A pool checkout on the controller loop's thread starts a record; a checkout on
  any other thread does nothing.
- ``CensusConnection.commit`` and ``rollback`` end it. They keep only the
  transactions that wrote (the connection is in a transaction only after a write
  or ``BEGIN IMMEDIATE``) or that tried to take the writer and failed
  (``mark_write_attempt``). They add the loop time since the record started, up to
  the end of SQLite's own commit or rollback (``fsync`` included), to the totals of
  the code location that ended the transaction, and start the next record.

The location is two frames: the first outside SQLAlchemy, ``contextlib`` and this
module, and its caller, so a write behind a shared transaction helper still names
who used the helper. The walk is bounded and keyed by ``(filename, line, name)``;
nothing is formatted until a report. The totals live in memory only. A location is
logged once when it first appears, a ranked summary at most every
``SUMMARY_INTERVAL_SECONDS`` while new writes arrive, and a final one at shutdown.

A write that cannot wait for another writer, because it takes the writer with a
zero busy timeout (``_try_reserve_write_lock``), is marked with
``mark_never_waits`` and counted apart: it cannot stall the loop on another
process's writer.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Callable

import sqlalchemy
from sqlalchemy import Engine, event

logger = logging.getLogger(__name__)

SUMMARY_INTERVAL_SECONDS = 300.0
# Sites listed in a summary, by total loop time.
SUMMARY_SITES = 25
# Frames inspected per write while looking for the first location outside the
# internal modules, and locations kept from there.
_MAX_FRAMES = 16
_SITE_DEPTH = 2

_Frame = tuple[str, int, str]
_Site = tuple[_Frame, ...]


_INTERNAL_PREFIXES = (
    os.path.dirname(os.path.abspath(sqlalchemy.__file__)) + os.sep,
    os.path.abspath(contextlib.__file__),
    os.path.abspath(__file__),
)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep

_lock = threading.Lock()
_loop_thread: int | None = None
# site -> [writes, total seconds, max seconds, never-waits writes]
_totals: dict[_Site, list[float]] = {}
_changed_since_summary = False
_last_summary_at = 0.0


def bind_loop_thread(thread_id: int | None = None) -> None:
    """Record the controller loop's thread; only transactions opened on it are counted."""

    global _loop_thread
    _loop_thread = threading.get_ident() if thread_id is None else thread_id


def unbind_loop_thread() -> None:
    """Stop counting: the loop no longer serves Sessions, so its writes stall none."""

    global _loop_thread
    _loop_thread = None


class CensusConnection(sqlite3.Connection):
    """A sqlite3 connection that ends the census's record of a loop transaction."""

    _census: list[Any] | None = None

    def commit(self) -> None:
        self._end(super().commit)

    def rollback(self) -> None:
        self._end(super().rollback)

    def _end(self, finish: Callable[[], None]) -> None:
        record = self._census
        if record is None or threading.get_ident() != _loop_thread:
            finish()
            return
        wrote = self.in_transaction or record[2]
        try:
            finish()
        finally:
            now = time.perf_counter()
            # The next transaction on this checkout starts where this one ended.
            self._census = [now, False, False]
            if wrote:
                _count(_site(sys._getframe(1)), now - record[0], never_waits=record[1])


def watch_engine(engine: Engine) -> None:
    event.listen(engine, "checkout", _on_checkout)


def _record(conn: Any) -> list[Any] | None:
    return getattr(conn.connection.dbapi_connection, "_census", None)


def mark_never_waits(conn: Any) -> None:
    """Mark the loop transaction on ``conn`` as one that never waits for another writer."""

    record = _record(conn)
    if record is not None:
        record[1] = True


def mark_write_attempt(conn: Any) -> None:
    """Count the loop transaction on ``conn`` even if taking the writer fails.

    A ``BEGIN IMMEDIATE`` that times out waiting for another writer leaves no
    transaction behind, yet the loop waited the whole busy timeout for it.
    """

    record = _record(conn)
    if record is not None:
        record[2] = True


def _on_checkout(dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
    if threading.get_ident() != _loop_thread or not isinstance(dbapi_connection, CensusConnection):
        return
    dbapi_connection._census = [time.perf_counter(), False, False]


def _site(frame: Any) -> _Site:
    found: list[_Frame] = []
    for _ in range(_MAX_FRAMES):
        if frame is None:
            break
        code = frame.f_code
        if not code.co_filename.startswith(_INTERNAL_PREFIXES):
            found.append((code.co_filename, frame.f_lineno, code.co_name))
            if len(found) == _SITE_DEPTH:
                break
        frame = frame.f_back
    return tuple(found)


def _count(site: _Site, elapsed: float, *, never_waits: bool) -> None:
    global _changed_since_summary
    with _lock:
        totals = _totals.get(site)
        first = totals is None
        if first:
            totals = _totals[site] = [0, 0.0, 0.0, 0]
        totals[0] += 1
        totals[1] += elapsed
        if elapsed > totals[2]:
            totals[2] = elapsed
        if never_waits:
            totals[3] += 1
        _changed_since_summary = True
    if first:
        logger.info(
            "Controller-loop SQLite write site: %s (%.1f ms%s)",
            _format_site(site),
            elapsed * 1000,
            ", never waits" if never_waits else "",
        )
    if time.monotonic() - _last_summary_at >= SUMMARY_INTERVAL_SECONDS:
        log_summary()


def log_summary(*, final: bool = False) -> None:
    """Log the write sites ranked by total loop time.

    A periodic summary is skipped when nothing was written since the last one; the
    final one at shutdown always reports the whole run, even a run with no writes.
    """

    global _changed_since_summary, _last_summary_at
    with _lock:
        if not (_changed_since_summary or final):
            return
        _changed_since_summary = False
        _last_summary_at = time.monotonic()
        ranked = sorted(_totals.items(), key=lambda item: item[1][1], reverse=True)
        writes = sum(int(totals[0]) for _site, totals in ranked)
    lines = [
        f"{int(count)} writes, {total * 1000:.0f} ms total, {peak * 1000:.1f} ms max"
        f"{f', {int(never)} never wait' if never else ''}: {_format_site(site)}"
        for site, (count, total, peak, never) in ranked[:SUMMARY_SITES]
    ]
    logger.info(
        "Controller-loop SQLite writers%s: %d writes at %d sites, by total loop time%s%s",
        " at shutdown" if final else "",
        writes,
        len(ranked),
        f" (top {SUMMARY_SITES})" if len(ranked) > SUMMARY_SITES else "",
        "".join(f"\n  {line}" for line in lines),
    )


def _format_site(site: _Site) -> str:
    if not site:
        return "unknown"
    return " <- ".join(
        f"{filename.removeprefix(_REPO_ROOT)}:{line} in {name}" for filename, line, name in site
    )
