"""The definition-status index keeps lifecycle probes bounded by live statuses."""

from __future__ import annotations

from importlib import import_module
import sqlite3
from pathlib import Path

import pytest

from storage import migrations
from storage.background import SQLiteBackgroundTaskStore
from storage.models import run_definitions


pytestmark = pytest.mark.no_sqlite_template

_REVISION = import_module(
    "storage.alembic.versions.20261107_0073_agent_runs_definition_status_index"
)


def _index_sql(path: Path, name: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?",
            (name,),
        ).fetchone()
    return str(row[0]) if row else None


def _normalized_sql(value: str | None) -> str | None:
    return (
        " ".join(str(value or "").split()).casefold().replace(" if not exists ", " ")
        if value is not None
        else None
    )


def test_definition_status_index_upgrades_and_downgrades_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path, revision=_REVISION.down_revision)
    assert _index_sql(path, _REVISION._INDEX) is None

    migrations.run_migrations(path)
    assert _normalized_sql(_index_sql(path, _REVISION._INDEX)) == _normalized_sql(
        _REVISION.CREATE_INDEX_SQL
    )

    from alembic import command

    command.downgrade(migrations.alembic_config(path), _REVISION.down_revision)
    assert _index_sql(path, _REVISION._INDEX) is None


def test_task_and_watch_lifecycle_probes_seek_definition_status_index(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "insert into run_definitions "
            "(id, definition_type, name, enabled, created_at, updated_at, metadata_json) "
            "values (?, ?, ?, 1, '2026-11-07T00:00:00Z', '2026-11-07T00:00:00Z', '{}')",
            [("task-hot", "scheduled", "task-hot"), ("watch-hot", "watch", "watch-hot")],
        )
        history = []
        for definition_id in ("task-hot", "watch-hot"):
            history.extend(
                (
                    f"{definition_id}-run-{index:05d}",
                    definition_id,
                    "scheduled" if definition_id == "task-hot" else "watch",
                    "queued" if index == 4999 else "succeeded",
                )
                for index in range(5000)
            )
        conn.executemany(
            "insert into agent_runs "
            "(id, definition_id, run_type, status, created_at, updated_at, metadata_json) "
            "values (?, ?, ?, ?, '2026-11-07T00:00:00Z', '2026-11-07T00:00:00Z', '{}')",
            history,
        )
        conn.execute("analyze")
        conn.commit()

    store = SQLiteBackgroundTaskStore(path, read_only=True)
    try:
        for definition_type in ("scheduled", "watch"):
            statement = store._definitions_query(definition_type).order_by(
                run_definitions.c.created_at, run_definitions.c.id
            )
            sql = str(statement.compile(store.engine, compile_kwargs={"literal_binds": True}))
            with sqlite3.connect(path) as conn:
                plan = [str(row[-1]) for row in conn.execute("explain query plan " + sql)]
            assert any(
                "SEARCH agent_runs USING INDEX ix_agent_runs_definition_status "
                "(definition_id=? AND status=?)" in detail
                for detail in plan
            ), plan
    finally:
        store.close()
