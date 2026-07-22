from __future__ import annotations

import sqlite3
from pathlib import Path

from memory_poc.research_inspection import inspect_isolated_root, inspect_retention


def test_research_inspection_reports_isolated_markdown_and_sqlite_without_gating(tmp_path: Path) -> None:
    root = tmp_path / "everos-root"
    markdown = root / "avibe" / "personal" / "users" / "owner" / "user.md"
    sqlite = root / ".index" / "sqlite" / "system.db"
    markdown.parent.mkdir(parents=True)
    sqlite.parent.mkdir(parents=True)
    markdown.write_text("synthetic memory", encoding="utf-8")
    sqlite.write_bytes(b"sqlite")

    inspection = inspect_isolated_root(root)

    assert inspection.markdown_present is True
    assert inspection.sqlite_present is True
    assert inspection.outcome == "observed"


def test_research_inspection_reports_missing_artifacts(tmp_path: Path) -> None:
    inspection = inspect_isolated_root(tmp_path / "everos-root")

    assert inspection.markdown_present is False
    assert inspection.sqlite_present is False
    assert inspection.outcome == "not_observed"


def test_retention_inspection_counts_only_storage_categories(tmp_path: Path) -> None:
    root = tmp_path / "everos-root"
    user_root = root / "avibe" / "personal" / "users" / "owner"
    (user_root / "episodes").mkdir(parents=True)
    (user_root / ".atomic_facts").mkdir()
    (user_root / "user.md").write_text("profile payload must not be read", encoding="utf-8")
    (user_root / "episodes" / "episode.md").write_text("episode payload must not be read", encoding="utf-8")
    (user_root / ".atomic_facts" / "facts.md").write_text("fact payload must not be read", encoding="utf-8")
    sqlite_path = root / ".index" / "sqlite" / "system.db"
    sqlite_path.parent.mkdir(parents=True)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("create table unprocessed_buffer (id text)")
        connection.execute("create table memcell (id text)")
        connection.execute("insert into unprocessed_buffer values ('one')")
        connection.execute("insert into unprocessed_buffer values ('two')")
        connection.execute("insert into memcell values ('one')")

    inspection = inspect_retention(root)

    assert inspection.unprocessed_buffer_rows == 2
    assert inspection.memcell_rows == 1
    assert inspection.profile_files == 1
    assert inspection.episode_files == 1
    assert inspection.atomic_fact_files == 1
    assert inspection.sqlite_files == 1
