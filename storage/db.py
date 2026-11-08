from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Callable, TypeVar

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, OperationalError

from config import paths
from storage import loop_writers

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Pauses between the attempts of ``retry_sqlite_write``: three attempts in all.
SQLITE_WRITE_RETRY_DELAYS = (0.05, 0.1)
_SQLITE_BUSY = 5


def escape_sql_like(value: str) -> str:
    """Escape the LIKE/ILIKE metacharacters in *value* for use with ``escape="\\"``.

    Backslash first (so the escapes we add aren't re-escaped), then the two
    wildcards ``%`` and ``_`` — a literal one in a user query must match itself,
    not widen the pattern. Shared by every LIKE-based search (session title +
    message content) so the escaping can't drift between call sites.
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_sqlite_path(db_path: Path | None = None) -> Path:
    return (db_path or paths.get_sqlite_state_path()).expanduser().resolve()


def sqlite_url(db_path: Path | None = None) -> str:
    path = _resolve_sqlite_path(db_path)
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def create_sqlite_engine(db_path: Path | None = None, *, read_only: bool = False) -> Engine:
    """Open normal state, or existing state without creation or journal changes."""
    path = _resolve_sqlite_path(db_path)
    if path.exists() and path.stat().st_size:
        # Explicit-path stores also use this factory, sometimes without the
        # importer. Reject incompatible epochs before even changing WAL mode.
        from storage.migrations import preflight_database

        identity = (path.stat().st_dev, path.stat().st_ino)
        with _cached_engine_lock:
            checked = _checked_sqlite_paths.get(path)
        if checked != identity:
            try:
                preflight_database(path, read_only=read_only)
            except sqlite3.Error as exc:
                # Factory callers already handle SQLAlchemy's DBAPI error
                # hierarchy. Preserve it when the earlier raw-SQLite admission
                # check observes corruption, busy state or a missing table.
                raise DBAPIError.instance(
                    "SQLite compatibility preflight", None, exc, sqlite3.Error, hide_parameters=True,
                ) from exc
            with _cached_engine_lock:
                _checked_sqlite_paths[path] = identity
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    # ``hide_parameters`` keeps bound values out of ``str(exc)`` on every
    # SQLAlchemy error. Those values are the user's identity and access-control
    # data -- emails, subjects, instance and scope ids, serialized authorization
    # claims -- and any caller that logs a database failure with a traceback
    # otherwise writes them into the unrotated service log. Owning it here rather
    # than at each log site is what makes that true for callers not yet written:
    # this is the only ``create_engine`` in the codebase, so every engine
    # inherits it. The statement text, the traceback, and the underlying sqlite3
    # error all survive, which is what a diagnostic actually needs.
    url = (
        URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        if read_only else sqlite_url(path)
    )
    # Every connection is a ``CensusConnection`` so the loop-writer census (#2710)
    # can time a loop transaction to the end of SQLite's own commit.
    engine = create_engine(
        url,
        future=True,
        hide_parameters=True,
        connect_args={"factory": loop_writers.CensusConnection},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        if read_only:
            cursor.execute("PRAGMA query_only = ON")
        else:
            with loop_writers.track_autocommit_write(dbapi_connection):
                cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    loop_writers.watch_engine(engine)
    return engine


def _is_sqlite_busy(error: BaseException) -> bool:
    """Whether ``error`` is SQLite writer contention: primary code ``SQLITE_BUSY``, ``BUSY_SNAPSHOT`` included.

    ``SQLITE_LOCKED`` ("database table is locked") is a conflict inside one connection or shared cache, which
    waiting does not end, so it is not contention. Python 3.10 has no ``sqlite_errorcode``; there the exact busy
    message stands for the code.
    """
    if isinstance(error, OperationalError):
        error = error.orig
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return str(error) == "database is locked"
    return code & 0xFF == _SQLITE_BUSY


def retry_sqlite_write(operation: Callable[[], _T]) -> _T:
    """Run a complete write transaction again while another writer holds SQLite (``docs/plans/sqlite-write-retries.md``).

    ``operation`` opens its own transaction, repeats every read it decides on, and rolls back before it raises, as
    ``engine.begin()`` does; it has no external effect, and nothing after its commit can fail. Each attempt can
    wait the full ``busy_timeout``, so three attempts bound the wait at about 15.15 s. Worker threads only: this
    sleeps between attempts.
    """
    for attempt, delay in enumerate(SQLITE_WRITE_RETRY_DELAYS, start=1):
        try:
            return operation()
        except Exception as exc:
            if not _is_sqlite_busy(exc):
                raise
            logger.warning(
                "SQLite writer busy; retrying %s (attempt %d of %d)",
                getattr(operation, "__qualname__", operation),
                attempt + 1,
                len(SQLITE_WRITE_RETRY_DELAYS) + 1,
            )
        time.sleep(delay)
    return operation()


_cached_engine_lock = RLock()
_cached_engines: dict[tuple[Path, bool], Engine] = {}
# Same process-lifetime contract as importer.ensure_sqlite_state. A replacement
# inode is rechecked; concurrently breaking upgrades require all writers stopped.
_checked_sqlite_paths: dict[Path, tuple[int, int]] = {}


def get_cached_sqlite_engine(db_path: Path | None = None, *, read_only: bool = False) -> Engine:
    """Return a process-local engine with a pool isolated by path and capability.

    ``create_sqlite_engine`` intentionally remains a fresh-engine factory for
    tests, migrations, and short one-off tools. Controller hot paths use this
    cache so repeated writes and binding-state reads do not allocate an engine
    and SQLite connection pool each time. Read-only and writable callers never
    share a pool; both follow the existing process/reset disposal lifecycle.
    """
    key = (_resolve_sqlite_path(db_path), read_only)
    with _cached_engine_lock:
        engine = _cached_engines.get(key)
        if engine is None:
            engine = create_sqlite_engine(key[0], read_only=read_only)
            _cached_engines[key] = engine
        return engine


def dispose_cached_sqlite_engines() -> None:
    """Dispose process-local cached SQLite engines.

    Production relies on process lifetime cleanup. Tests call this around
    isolated homes so cached connections never point at a previous test's state.
    """
    with _cached_engine_lock:
        engines = list(_cached_engines.values())
        _cached_engines.clear()
        _checked_sqlite_paths.clear()
    for engine in engines:
        engine.dispose()


class SqliteInvalidationProbe:
    """Detect external SQLite writes with PRAGMA data_version.

    SQLite data_version values are only meaningful when compared across
    repeated calls on the same connection, so this probe keeps one dedicated
    connection for its lifetime.
    """

    def __init__(self, engine: Engine):
        self._connection = engine.connect()
        try:
            self._last_data_version = int(
                self._connection.exec_driver_sql("PRAGMA data_version").scalar_one()
            )
        except BaseException:
            self._connection.close()
            raise
        self._lock = RLock()

    def has_external_write(self) -> bool:
        # Runtime work lanes may ask the same store to refresh from separate
        # executor workers. The probe deliberately owns one persistent
        # connection, so its read-and-compare operation must remain single-file.
        with self._lock:
            version = int(
                self._connection.exec_driver_sql("PRAGMA data_version").scalar_one()
            )
            changed = version != self._last_data_version
            self._last_data_version = version
            return changed

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SqliteInvalidationProbe":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
