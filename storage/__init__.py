"""SQLite-backed state storage infrastructure."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import create_sqlite_engine, sqlite_url
    from .importer import MigrationImportReport, ensure_sqlite_state
    from .migrations import run_migrations


def __getattr__(name: str):
    # Pure contracts such as pagination do not need a database or migrations.
    if name in {"create_sqlite_engine", "sqlite_url"}:
        from . import db

        return getattr(db, name)
    if name in {"MigrationImportReport", "ensure_sqlite_state"}:
        from . import importer

        return getattr(importer, name)
    if name == "run_migrations":
        from .migrations import run_migrations

        return run_migrations
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "MigrationImportReport",
    "create_sqlite_engine",
    "ensure_sqlite_state",
    "run_migrations",
    "sqlite_url",
]
