from __future__ import annotations

from pathlib import Path

from memory_poc.research_inspection import inspect_isolated_root


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
