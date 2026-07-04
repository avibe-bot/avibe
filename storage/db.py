from __future__ import annotations

from pathlib import Path
from threading import RLock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL

from config import paths


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


def create_sqlite_engine(db_path: Path | None = None) -> Engine:
    path = _resolve_sqlite_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(path), future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    return engine


_cached_engine_lock = RLock()
_cached_engines: dict[Path, Engine] = {}


def get_cached_sqlite_engine(db_path: Path | None = None) -> Engine:
    """Return a process-local SQLite engine for hot write paths.

    ``create_sqlite_engine`` intentionally remains a fresh-engine factory for
    tests, migrations, and short one-off tools. Controller hot paths should use
    this cache so every emitted chunk or queue tick does not allocate a new
    SQLAlchemy engine and SQLite connection pool.
    """
    path = _resolve_sqlite_path(db_path)
    with _cached_engine_lock:
        engine = _cached_engines.get(path)
        if engine is None:
            engine = create_sqlite_engine(path)
            _cached_engines[path] = engine
        return engine


def dispose_cached_sqlite_engines() -> None:
    """Dispose process-local cached SQLite engines.

    Production relies on process lifetime cleanup. Tests call this around
    isolated homes so cached connections never point at a previous test's state.
    """
    with _cached_engine_lock:
        engines = list(_cached_engines.values())
        _cached_engines.clear()
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
        self._last_data_version: int | None = None

    def has_external_write(self) -> bool:
        version = int(self._connection.exec_driver_sql("PRAGMA data_version").scalar_one())
        changed = self._last_data_version is not None and version != self._last_data_version
        self._last_data_version = version
        return changed

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteInvalidationProbe":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
