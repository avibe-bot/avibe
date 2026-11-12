"""Bounded SQLite query-planner maintenance.

SQLite recommends running ``PRAGMA optimize=0x10002`` when a long-lived
connection is first opened, then ``PRAGMA optimize`` periodically. The startup
mask examines tables that have no query history on the fresh maintenance
connection.

Avibe's maintenance engine is pooled, so a daily pass is not guaranteed to
reuse the connection that saw Activity queries. It therefore uses the all-table
mask too. SQLite 3.46 added that mask and its automatic temporary limit. On
SQLite 3.32 through 3.45, the explicit analysis limit plus a fallback
``ANALYZE`` keeps the all-table pass bounded. Older SQLite builds are reported
as unsupported rather than silently claiming that statistics were refreshed.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import Engine

ANALYSIS_LIMIT = 1000
STARTUP_OPTIMIZE_MASK = "0x10002"
_MIN_ANALYSIS_LIMIT_VERSION = (3, 32, 0)
_ALL_TABLE_OPTIMIZE_VERSION = (3, 46, 0)


def _parse_sqlite_version(value: object) -> tuple[int, int, int] | None:
    parts = str(value).split(".")
    if len(parts) < 3:
        return None
    try:
        return tuple(int(part) for part in parts[:3])
    except ValueError:
        return None


def _is_read_only_engine(engine: Engine) -> bool:
    query = getattr(getattr(engine, "url", None), "query", {})
    return str(query.get("mode", "")).lower() == "ro"


def run(
    engine: Engine,
    *,
    startup: bool,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one bounded optimizer pass on a writable maintenance connection.

    The caller owns cadence and must invoke this from a worker thread. This
    function never installs a per-connection SQLAlchemy hook. A cancellation
    already observed before the pass avoids opening the connection; cancellation
    during SQLite's bounded statement is observed before reporting success, but
    cannot interrupt that statement.
    """
    if _is_read_only_engine(engine):
        return {"status": "read_only"}
    if cancel_event is not None and cancel_event.is_set():
        return {"status": "cancelled"}

    with engine.begin() as conn:
        raw_version = conn.exec_driver_sql("SELECT sqlite_version()").scalar_one()
        sqlite_version = _parse_sqlite_version(raw_version)
        if sqlite_version is None or sqlite_version < _MIN_ANALYSIS_LIMIT_VERSION:
            return {"status": "unsupported", "sqlite_version": str(raw_version)}

        optimize = f"PRAGMA optimize={STARTUP_OPTIMIZE_MASK}"
        conn.exec_driver_sql(f"PRAGMA analysis_limit={ANALYSIS_LIMIT}")
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled"}
        conn.exec_driver_sql(optimize)

        # SQLite 3.46 added the 0x10000 all-table mask. Older supported
        # versions still understand PRAGMA optimize, but cannot use that bit;
        # ANALYZE with analysis_limit supplies the explicit bounded fallback.
        fallback_analyze = sqlite_version < _ALL_TABLE_OPTIMIZE_VERSION
        if fallback_analyze:
            if cancel_event is not None and cancel_event.is_set():
                return {"status": "cancelled"}
            conn.exec_driver_sql("ANALYZE")

    if cancel_event is not None and cancel_event.is_set():
        return {"status": "cancelled"}
    return {
        "status": "ok",
        "startup": startup,
        "sqlite_version": str(raw_version),
        "analysis_limit": ANALYSIS_LIMIT,
        "optimize": optimize,
        "fallback_analyze": fallback_analyze,
    }
