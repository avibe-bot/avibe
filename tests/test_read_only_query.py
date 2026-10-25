from __future__ import annotations

import sqlite3
from pathlib import Path

from storage.pagination import PageRequest
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query


def _seed_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("create table runs (id text primary key, status text, created_at text)")
        for index in range(25):
            conn.execute(
                "insert into runs (id, status, created_at) values (?, ?, ?)",
                (f"run-{index:02d}", "succeeded", f"2026-05-25T00:{index:02d}:00+00:00"),
            )
        conn.commit()


def test_read_only_query_pages_select_results(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_db(db_path)

    result = run_read_only_query(
        "select id, status from runs order by created_at desc",
        db_path=db_path,
        page_request=PageRequest(page=1, limit=20),
    )

    assert result.columns == ["id", "status"]
    assert len(result.rows) == 20
    assert result.rows[0]["id"] == "run-24"
    assert result.pagination.has_more is True
    assert result.pagination.next_page == 2


def test_read_only_query_rejects_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_db(db_path)

    try:
        run_read_only_query("delete from runs", db_path=db_path, page_request=PageRequest())
    except ReadOnlyQueryError as exc:
        assert exc.code == "query_failed"
    else:
        raise AssertionError("write SQL should be rejected")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from runs").fetchone()[0] == 25


def test_read_only_query_allows_only_schema_pragmas(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_db(db_path)

    for sql in ("PRAGMA table_info(runs)", "select name from pragma_table_info('runs')"):
        result = run_read_only_query(sql, db_path=db_path, page_request=PageRequest())
        assert [row["name"] for row in result.rows] == ["id", "status", "created_at"]

    for sql in ("pragma query_only = off", "pragma writable_schema = on", "select * from pragma_query_only"):
        try:
            run_read_only_query(sql, db_path=db_path, page_request=PageRequest())
        except ReadOnlyQueryError as exc:
            assert exc.code == "query_failed"
            assert exc.hint.startswith("Only schema PRAGMAs are allowed")
        else:
            raise AssertionError(f"{sql!r} should be rejected")


def test_read_only_query_rejects_multiple_statements(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    _seed_db(db_path)

    try:
        run_read_only_query("select 1; select 2", db_path=db_path, page_request=PageRequest())
    except ReadOnlyQueryError as exc:
        assert exc.code == "query_failed"
    else:
        raise AssertionError("multiple statements should be rejected")
