"""Measure which SQLite write transactions run on the controller loop (#2710).

A write on the controller loop stalls every Session while it waits for SQLite's
writer or for its own commit, and the writers are reached through so many
indirections that no reviewed list of them stays complete. So the engine factory
counts them instead. ``storage.db.create_sqlite_engine`` attaches two listeners to
every engine:

- ``begin`` notes the start time of a transaction opened on the controller loop's
  thread, and does nothing on any other thread;
- ``commit`` and ``rollback`` keep only the transactions that wrote (the DBAPI
  connection is in a transaction only after a write or ``BEGIN IMMEDIATE``), and
  add their loop time to the totals of the code location that ended them.

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
import sys
import threading
import time
from typing import Any

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

_OPEN_KEY = "avibe_loop_write"

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


def watch_engine(engine: Engine) -> None:
    event.listen(engine, "begin", _on_begin)
    event.listen(engine, "commit", _on_end)
    event.listen(engine, "rollback", _on_end)


def mark_never_waits(conn: Any) -> None:
    """Mark the loop transaction on ``conn`` as one that never waits for another writer."""

    record = conn.info.get(_OPEN_KEY)
    if record is not None:
        record[1] = True


def _on_begin(conn: Any) -> None:
    if threading.get_ident() != _loop_thread:
        return
    conn.info[_OPEN_KEY] = [time.perf_counter(), False]


def _on_end(conn: Any) -> None:
    if threading.get_ident() != _loop_thread:
        return
    record = conn.info.pop(_OPEN_KEY, None)
    if record is None or not conn.connection.dbapi_connection.in_transaction:
        return
    elapsed = time.perf_counter() - record[0]
    _count(_site(sys._getframe(1)), elapsed, never_waits=record[1])


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
    final one at shutdown always reports the whole run.
    """

    global _changed_since_summary, _last_summary_at
    with _lock:
        if not (_changed_since_summary or (final and _totals)):
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
        "Controller-loop SQLite writers%s: %d writes at %d sites, by total loop time%s\n  %s",
        " at shutdown" if final else "",
        writes,
        len(ranked),
        f" (top {SUMMARY_SITES})" if len(ranked) > SUMMARY_SITES else "",
        "\n  ".join(lines),
    )


def _format_site(site: _Site) -> str:
    if not site:
        return "unknown"
    return " <- ".join(
        f"{filename.removeprefix(_REPO_ROOT)}:{line} in {name}" for filename, line, name in site
    )
