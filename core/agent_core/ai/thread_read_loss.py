"""Optional measured-read stop: route/model digest -> expiry. No successful measurements or IDs."""

import math
import sqlite3
import time
from contextlib import closing

from config.paths import get_state_dir

DAY = 86400
_live: dict[bytes, float] = {}  # Still authoritative if a loss write was busy. Called under the admission lock.


def stop_reason(route: bytes) -> str | None:
    if _live.get(route, 0) > time.time():
        return "bootstrap_read_loss"
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        if not path.exists():
            return None
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("BEGIN")
            tables = dict.fromkeys(
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name IN ('route_loss','hint')")
            )
            stops = []
            if "route_loss" in tables:
                stops.extend(db.execute("SELECT until FROM route_loss WHERE route=?", (route,)))
            if "hint" in tables:
                # Preserve the released schema's last measured loss; ignore its retired day/count fields.
                stops.extend(db.execute("SELECT until FROM hint WHERE id=1 AND loss=?", (route,)))
            now = time.time()
            for (until,) in stops:
                if type(until) not in (int, float) or not math.isfinite(until):
                    return "bootstrap_stop_unavailable"
                if until > now:
                    return "bootstrap_read_loss"
    except (sqlite3.Error, OSError, ValueError):
        return "bootstrap_stop_unavailable"  # Ordinary full remains available; preview never writes or repairs.
    return None


def record(route: bytes) -> None:
    now = time.time()
    for expired in [key for key, expiry in _live.items() if expiry <= now]:
        del _live[expired]
    until = _live[route] = max(_live.get(route, 0), now + DAY)
    try:
        path = get_state_dir() / "message-thread-bootstrap.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS route_loss (route BLOB PRIMARY KEY, until REAL NOT NULL)")
            db.execute("DELETE FROM route_loss WHERE until<=?", (now,))
            db.execute(
                "INSERT INTO route_loss VALUES (?,?) ON CONFLICT(route) DO UPDATE SET until=max(until,excluded.until)",
                (route, until),
            )
    except (sqlite3.Error, OSError, ValueError):
        pass  # An unwritable stop still protects this process, but cannot survive its loss.
