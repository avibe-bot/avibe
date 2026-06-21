from __future__ import annotations

from pathlib import Path

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


def sqlite_url(db_path: Path | None = None) -> str:
    path = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def create_sqlite_engine(db_path: Path | None = None) -> Engine:
    path = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
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
