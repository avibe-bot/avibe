"""repair scope_id nullability skipped by a historical schema stamp

Some head-shaped databases were stamped past 20260723_0032 without running its
table alterations. Rebuilding these tables now would copy large histories and
temporarily delete referenced rows under SQLite batch mode. Instead, remove only
the stale ``NOT NULL`` tokens from SQLite's stored CREATE TABLE statements,
increment ``schema_version``, and leave every table root page and row untouched.

Downgrade is deliberately a no-op. Scope-less Sessions can create rows after
this repair, so tightening the columns again would require deleting valid data
or make the downgrade fail.

Revision ID: 20261016_0069
Revises: 20261016_0068
Create Date: 2026-10-16
"""

from __future__ import annotations

import sqlite3

from alembic import op

revision = "20261016_0069"
down_revision = "20261016_0068"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"

_SCOPE_ID_TABLES = ("messages", "agent_events", "media_objects")
_VISIBILITY_INDEX = "ix_agent_sessions_visibility"
_VISIBILITY_INDEX_SQL = (
    f"create index {_VISIBILITY_INDEX} on agent_sessions (visibility)"
)


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_schema where type = 'table'"
        )
    }


def _columns(bind, table: str) -> dict[str, tuple]:
    return {
        str(row[1]): tuple(row)
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def _indexes(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma index_list("{table}")').fetchall()
    }


def _table_item_spans(sql: str) -> list[tuple[int, int]]:
    """Return top-level column/constraint spans from one CREATE TABLE statement."""

    spans: list[tuple[int, int]] = []
    depth = 0
    item_start: int | None = None
    quote_end: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote_end is not None:
            if char == quote_end:
                if index + 1 < len(sql) and sql[index + 1] == quote_end:
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote_end = char
        elif char == "[":
            quote_end = "]"
        elif char == "(":
            depth += 1
            if depth == 1:
                item_start = index + 1
        elif char == ")":
            if depth == 1:
                if item_start is None:
                    raise RuntimeError("CREATE TABLE body has no item start")
                spans.append((item_start, index))
                return spans
            depth -= 1
        elif char == "," and depth == 1:
            if item_start is None:
                raise RuntimeError("CREATE TABLE body has no item start")
            spans.append((item_start, index))
            item_start = index + 1
        index += 1
    raise RuntimeError("CREATE TABLE statement has no complete body")


def _leading_identifier(item: str) -> tuple[str, int]:
    index = 0
    while index < len(item) and item[index].isspace():
        index += 1
    if index == len(item):
        return "", index

    opener = item[index]
    quote_end = {'"': '"', "`": "`", "[": "]"}.get(opener)
    if quote_end is not None:
        index += 1
        value: list[str] = []
        while index < len(item):
            char = item[index]
            if char == quote_end:
                if index + 1 < len(item) and item[index + 1] == quote_end:
                    value.append(char)
                    index += 2
                    continue
                return "".join(value), index + 1
            value.append(char)
            index += 1
        raise RuntimeError("unterminated quoted identifier in CREATE TABLE")

    start = index
    while index < len(item) and (
        item[index].isalnum() or item[index] in {"_", "$"}
    ):
        index += 1
    return item[start:index], index


def _top_level_words(item: str, start: int) -> list[tuple[str, int, int]]:
    words: list[tuple[str, int, int]] = []
    depth = 0
    quote_end: str | None = None
    index = start
    while index < len(item):
        char = item[index]
        if quote_end is not None:
            if char == quote_end:
                if index + 1 < len(item) and item[index + 1] == quote_end:
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote_end = char
            index += 1
            continue
        if char == "[":
            quote_end = "]"
            index += 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            word_start = index
            index += 1
            while index < len(item) and (
                item[index].isalnum() or item[index] in {"_", "$"}
            ):
                index += 1
            words.append((item[word_start:index].casefold(), word_start, index))
            continue
        index += 1
    return words


def _make_scope_id_nullable(table: str, sql: str) -> str:
    for start, end in _table_item_spans(sql):
        item = sql[start:end]
        identifier, identifier_end = _leading_identifier(item)
        if identifier.casefold() != "scope_id":
            continue

        words = _top_level_words(item, identifier_end)
        pairs = [
            (first[1], second[2])
            for first, second in zip(
                words,
                words[1:],
                strict=False,
            )
            if first[0] == "not" and second[0] == "null"
        ]
        if len(pairs) != 1:
            raise RuntimeError(
                f"{table}.scope_id is NOT NULL but its stored definition "
                f"contains {len(pairs)} top-level NOT NULL clauses"
            )
        remove_start, remove_end = pairs[0]
        while remove_start > identifier_end and item[remove_start - 1].isspace():
            remove_start -= 1
        updated_item = item[:remove_start] + item[remove_end:]
        return sql[:start] + updated_item + sql[end:]
    raise RuntimeError(f"{table} has no scope_id column definition")


def _table_info_from_sql(table: str, sql: str) -> list[tuple]:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(sql)
        return conn.execute(f'pragma table_info("{table}")').fetchall()


def _preflight_schema_edit(table: str, old_sql: str, new_sql: str) -> None:
    old_columns = _table_info_from_sql(table, old_sql)
    new_columns = _table_info_from_sql(table, new_sql)
    expected = [
        tuple((*row[:3], 0, *row[4:])) if row[1] == "scope_id" else tuple(row)
        for row in old_columns
    ]
    if new_columns != expected:
        raise RuntimeError(
            f"{table}.scope_id repair would change more than its NOT NULL flag"
        )


def _rewrite_drifted_tables(bind, drifted: list[str]) -> None:
    rewrites: dict[str, tuple[str, str]] = {}
    for table in drifted:
        row = bind.exec_driver_sql(
            "select sql from sqlite_schema where type = 'table' and name = ?",
            (table,),
        ).one_or_none()
        if row is None or not row[0]:
            raise RuntimeError(f"cannot read stored CREATE TABLE SQL for {table}")
        old_sql = str(row[0])
        new_sql = _make_scope_id_nullable(table, old_sql)
        _preflight_schema_edit(table, old_sql, new_sql)
        rewrites[table] = (old_sql, new_sql)

    schema_version = int(
        bind.exec_driver_sql("pragma schema_version").scalar_one()
    )
    bind.exec_driver_sql("pragma writable_schema = on")
    try:
        for table, (old_sql, new_sql) in rewrites.items():
            bind.exec_driver_sql(
                "update sqlite_schema set sql = ? "
                "where type = 'table' and name = ? and sql = ?",
                (new_sql, table, old_sql),
            )
            stored = bind.exec_driver_sql(
                "select sql from sqlite_schema where type = 'table' and name = ?",
                (table,),
            ).scalar_one()
            if stored != new_sql:
                raise RuntimeError(f"stored CREATE TABLE SQL changed concurrently for {table}")
        bind.exec_driver_sql(f"pragma schema_version = {schema_version + 1}")
    finally:
        bind.exec_driver_sql("pragma writable_schema = off")

    for table in drifted:
        if _columns(bind, table)["scope_id"][3]:
            raise RuntimeError(f"{table}.scope_id remained NOT NULL after repair")


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    drifted = [
        table
        for table in _SCOPE_ID_TABLES
        if table in tables
        and "scope_id" in _columns(bind, table)
        and _columns(bind, table)["scope_id"][3]
    ]
    if drifted:
        _rewrite_drifted_tables(bind, drifted)

    if (
        "agent_sessions" in tables
        and "visibility" in _columns(bind, "agent_sessions")
        and _VISIBILITY_INDEX not in _indexes(bind, "agent_sessions")
    ):
        bind.exec_driver_sql(_VISIBILITY_INDEX_SQL)


def downgrade() -> None:
    """Preserve scope-less rows; reapplying NOT NULL would be destructive."""

    return
