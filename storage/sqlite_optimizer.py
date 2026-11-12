"""Bounded SQLite query-planner maintenance.

SQLite recommends running ``PRAGMA optimize=0x10002`` when a long-lived
connection is first opened, then ``PRAGMA optimize`` periodically. The startup
mask examines tables that have no query history on the fresh maintenance
connection. The periodic call uses the connection's accumulated query history.

The analysis limit is deliberately set on the maintenance connection before
optimization. SQLite 3.46 and newer apply their own temporary bound, while
older supported versions use this explicit bound for approximate ANALYZE. Older
SQLite versions ignore unknown PRAGMAs, so the maintenance pass remains
forward-compatible with the versions Avibe can open.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import Engine

ANALYSIS_LIMIT = 1000
STARTUP_OPTIMIZE_MASK = "0x10002"


def run(
    engine: Engine,
    *,
    startup: bool,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one bounded optimizer pass on a writable maintenance connection.

    The caller owns cadence and must invoke this from a worker thread. This
    function never runs on read-only engines and never installs a per-connection
    SQLAlchemy hook. A cancellation already observed before the pass avoids
    opening the connection; cancellation during SQLite's bounded statement is
    observed before reporting success, but cannot interrupt that statement.
    """
    if cancel_event is not None and cancel_event.is_set():
        return {"status": "cancelled"}

    optimize = f"PRAGMA optimize={STARTUP_OPTIMIZE_MASK}" if startup else "PRAGMA optimize"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA analysis_limit={ANALYSIS_LIMIT}")
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled"}
        conn.exec_driver_sql(optimize)

    if cancel_event is not None and cancel_event.is_set():
        return {"status": "cancelled"}
    return {
        "status": "ok",
        "startup": startup,
        "analysis_limit": ANALYSIS_LIMIT,
        "optimize": optimize,
    }
