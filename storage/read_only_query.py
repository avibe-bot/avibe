from __future__ import annotations

import difflib
import re
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

# The PRAGMAs a query may run: each only describes the schema and has no setter.
# Every other PRAGMA stays denied: some change connection state, such as ``query_only``.
SCHEMA_PRAGMAS = frozenset(
    {"table_list", "table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
)

# Harness concepts whose table name shares nothing a fuzzy match could find.
_CONCEPT_TABLES = {
    "task": "run_definitions",
    "tasks": "run_definitions",
    "scheduled_tasks": "run_definitions",
    "watch": "run_definitions",
    "watches": "run_definitions",
}

_NO_SUCH_TABLE = re.compile(r"no such table: (?:main\.)?(\S+)")
_NO_SUCH_COLUMN = re.compile(r"no such column: (\S+)")
_TABLE_REFERENCE = re.compile(
    r"\b(?:from|join)\s+(?:main\.)?[\"`\[]?(\w+)[\"`\]]?(?:\s+(?:as\s+)?(\w+))?", re.IGNORECASE
)
_SQL_KEYWORDS = frozenset(
    {
        "as", "cross", "except", "full", "group", "having", "inner", "intersect", "join", "left",
        "limit", "natural", "on", "order", "outer", "right", "union", "using", "where", "window",
    }
)
_COLUMNS_HINT = "Read a table's columns with PRAGMA table_info(<table>)."


class ReadOnlyQueryError(ValueError):
    def __init__(self, message: str, *, code: str = "query_failed", hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


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
    user_context=None,
) -> QueryResult:
    from storage.resource_access_service import resolve_resource_access_context

    allow_skill_statistics = resolve_resource_access_context(user_context).can_manage_instance
    statement = _validate_single_statement(sql)
    resolved_db_path = db_path or paths.get_sqlite_state_path()
    if db_path is None:
        ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    uri = f"file:{quote(str(resolved_db_path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    steps = 0
    denials: list[tuple[int, str | None]] = []

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 1 if steps > step_limit else 0

    try:
        conn.execute("PRAGMA query_only = ON")
        def authorize(action, arg1, arg2, db_name, source):
            # SQL has no resource-row filter. Raw Skill observations share
            # agent_events, so roles below Member cannot query that table wholesale.
            if (not allow_skill_statistics and action == sqlite3.SQLITE_READ
                    and arg1 in {"skill_usage_daily", "agent_events"}):
                verdict = sqlite3.SQLITE_DENY
            else:
                verdict = _authorizer(action, arg1, arg2, db_name, source)
            if verdict == sqlite3.SQLITE_DENY:
                denials.append((action, arg1))
            return verdict

        conn.set_authorizer(authorize)
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
        conn.set_progress_handler(None, 0)
        hint = _error_hint(conn, statement, message, denials)
        raise ReadOnlyQueryError(message, code=code, hint=hint) from exc
    finally:
        conn.close()


def _error_hint(
    conn: sqlite3.Connection,
    statement: str,
    message: str,
    denials: list[tuple[int, str | None]],
) -> str | None:
    """What to change so the next attempt can succeed, when the error alone does not say."""

    if denials:
        action, name = denials[0]
        if action == sqlite3.SQLITE_PRAGMA:
            return (
                "Only schema PRAGMAs are allowed: " + ", ".join(sorted(SCHEMA_PRAGMAS)) + ". "
                + _COLUMNS_HINT
            )
        if action == sqlite3.SQLITE_READ:
            return f"Rows of '{name}' are not readable through vibe data query."
        return "vibe data query is read-only: run one SELECT or WITH statement, or a schema PRAGMA."
    table = _NO_SUCH_TABLE.search(message)
    column = _NO_SUCH_COLUMN.search(message)
    if not (table or column):
        return None
    try:
        schema = _schema_columns(conn)
    except sqlite3.DatabaseError:
        # The hint is advice; the query's own error is the answer and must survive.
        return None
    if table:
        return _unknown_table_hint(schema, table.group(1))
    return _unknown_column_hint(schema, statement, column.group(1))


def _schema_columns(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every queryable table's columns, in declaration order."""

    columns: dict[str, list[str]] = {}
    rows = conn.execute(
        "select m.name, p.name from sqlite_master m join pragma_table_info(m.name) p "
        "where m.type = 'table' and m.name not like 'sqlite_%' order by m.name, p.cid"
    )
    for table, column in rows:
        if table not in _DENIED_TABLES:
            columns.setdefault(table, []).append(column)
    return columns


def _similar(name: str, candidates: list[str]) -> list[str]:
    lowered = name.lower()
    matches = [candidate for candidate in candidates if lowered in candidate.lower()]
    matches += difflib.get_close_matches(name, candidates, n=3, cutoff=0.75)
    return list(dict.fromkeys(matches))[:3]


def _unknown_table_hint(schema: dict[str, list[str]], name: str) -> str:
    tables = list(schema)
    similar = _similar(name, tables)
    concept = _CONCEPT_TABLES.get(name.lower())
    if concept in schema and concept not in similar:
        similar.insert(0, concept)
    parts = [f"No table named '{name}'."]
    if similar:
        parts.append(f"Did you mean {', '.join(similar)}?")
    parts.append(f"Tables: {', '.join(tables)}.")
    parts.append(_COLUMNS_HINT)
    return " ".join(parts)


def _unknown_column_hint(schema: dict[str, list[str]], statement: str, reference: str) -> str:
    qualifier, _, column = reference.rpartition(".")
    aliases: dict[str, str] = {}
    referenced: list[str] = []
    for match in _TABLE_REFERENCE.finditer(statement):
        table, alias = match.group(1), match.group(2)
        if table not in schema:
            continue
        referenced.append(table)
        aliases[table] = table
        if alias and alias.lower() not in _SQL_KEYWORDS:
            aliases[alias] = table
    if qualifier in aliases:
        referenced = [aliases[qualifier]]
    referenced = list(dict.fromkeys(referenced))
    owners = [table for table, columns in schema.items() if column in columns]
    parts = []
    if referenced:
        parts.append(f"No column '{column}' on {', '.join(referenced)}.")
        similar = _similar(column, [name for table in referenced for name in schema[table]])
        if similar:
            parts.append(f"Did you mean {', '.join(similar)}?")
    else:
        parts.append(f"No column '{column}' on the tables this query reads.")
    if owners:
        parts.append(f"'{column}' is a column of {', '.join(owners)}.")
    for table in referenced:
        parts.append(f"{table} columns: {', '.join(schema[table])}.")
    if not referenced:
        parts.append(_COLUMNS_HINT)
    return " ".join(parts)


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
        # ``arg1`` is the PRAGMA's name as typed, for both the statement and the
        # ``pragma_table_info(...)`` table-valued form.
        return sqlite3.SQLITE_OK if (arg1 or "").lower() in SCHEMA_PRAGMAS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
