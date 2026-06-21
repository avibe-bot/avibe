from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from config import paths
from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
from storage.pagination import PageRequest, PageResult, page_result_from_limit_plus_one

DEFAULT_QUERY_STEP_LIMIT = 250_000

# Tables whose rows must never be readable through ``vibe data query``. Vault secret
# ciphertext is encrypted anyway, but there's no reason to expose it (or let backups
# of a query result carry it); names/policy live behind the proper Vaults API/CLI.
# Sibling vault tables (requests/links/grants/audit) stay queryable for inspection.
_DENIED_TABLES = frozenset({"vault_secrets"})


class ReadOnlyQueryError(ValueError):
    def __init__(self, message: str, *, code: str = "query_failed"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    pagination: PageResult[dict[str, Any]]


def run_read_only_query(
    sql: str,
    *,
    page_request: PageRequest | None,
    db_path: Path | None = None,
    step_limit: int = DEFAULT_QUERY_STEP_LIMIT,
) -> QueryResult:
    statement = _validate_single_statement(sql)
    resolved_db_path = db_path or paths.get_sqlite_state_path()
    if db_path is None:
        ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    uri = f"file:{quote(str(resolved_db_path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > step_limit else 0

    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(progress, 1000)
        cursor = conn.execute(statement)
        columns = [item[0] for item in cursor.description or []]
        if page_request is None:
            raw_rows = cursor.fetchall()
        else:
            skip = page_request.offset
            while skip > 0:
                chunk = cursor.fetchmany(min(skip, 1000))
                if not chunk:
                    break
                skip -= len(chunk)
            raw_rows = cursor.fetchmany(page_request.limit + 1)
        rows = [dict(row) for row in raw_rows]
        page = page_result_from_limit_plus_one(rows, page_request)
        return QueryResult(columns=columns, rows=page.items, pagination=page)
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        code = "query_interrupted" if "interrupted" in message.lower() else "query_failed"
        raise ReadOnlyQueryError(message, code=code) from exc
    finally:
        conn.close()


def _validate_single_statement(sql: str) -> str:
    statement = (sql or "").strip()
    if not statement:
        raise ReadOnlyQueryError("SQL statement is required.", code="missing_sql")
    return statement


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None) -> int:
    del arg2, db_name, source
    # SQLITE_READ fires per column access with the table name in ``arg1``; deny any
    # read touching a denylisted table so the whole statement fails with a clear error
    # rather than leaking the table or returning partial rows.
    if action == sqlite3.SQLITE_READ and arg1 in _DENIED_TABLES:
        return sqlite3.SQLITE_DENY
    denied_actions = {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_UPDATE,
    }
    if action in denied_actions:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
