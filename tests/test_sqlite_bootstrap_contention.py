"""Ordinary SQLite bootstrap must not turn report reads into writer contention.

The regression is a fresh CLI process opening an already-imported database:
completed migration markers used to take BEGIN IMMEDIATE, then hold that slot
through history-sized report counts. Existing migration races cover real repairs,
not the no-op open or the diagnostic tail after a repair.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from storage import importer, resource_access_service as resources
from storage.db import create_sqlite_engine
from storage.models import state_meta


@pytest.mark.parametrize("unavailable", [False, True])
def test_completed_resource_migration_is_readable_while_another_writer_owns_sqlite(
    tmp_path, sqlite_schema_db_factory, monkeypatch, unavailable,
):
    db = tmp_path / "state.sqlite"
    sqlite_schema_db_factory(db)
    engine = create_sqlite_engine(db)
    peer = create_sqlite_engine(db)
    marker = json.dumps({"state": "completed", "schema_version": 3, "instance_id": "already-bound"})
    configured_status = (
        resources.RESOURCE_BINDING_STATE_UNAVAILABLE if unavailable else resources.RESOURCE_BINDING_STATE_UNPAIRED
    )
    monkeypatch.setattr(resources, "_configured_resource_state", lambda: resources.ConfiguredResourceState(
        status=configured_status, instance_id=None, instance_kind=None, runtime_ready=False,
    ))
    try:
        with engine.begin() as conn:
            conn.execute(state_meta.insert().values(
                key=resources.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY, value_json=marker, updated_at="before",
            ))
        with peer.begin() as writer:
            writer.exec_driver_sql("BEGIN IMMEDIATE")
            with engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA busy_timeout = 0")
                result = resources.migrate_legacy_deferred_resource_contexts(conn)
                assert not conn.connection.dbapi_connection.in_transaction
                assert result == {
                    "legacy_deferred_definitions": 0,
                    "legacy_deferred_runs": 0,
                    "legacy_deferred_deliveries": 0,
                    "binding_status": (
                        resources.RESOURCE_BINDING_STATE_UNAVAILABLE if unavailable else resources.RESOURCE_BINDING_STATE_SEALED
                    ),
                }
                assert conn.execute(select(state_meta.c.value_json).where(
                    state_meta.c.key == resources.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
                )).scalar_one() == marker
    finally:
        peer.dispose()
        engine.dispose()


def test_warm_bootstrap_releases_migration_writer_before_report_counts(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    db = state_dir / "state.sqlite"
    importer.ensure_sqlite_state(db_path=db, state_dir=state_dir)
    engine = create_sqlite_engine(db)
    # An unfinished pairing migration must still WRITE its conservative seal.
    with engine.begin() as conn:
        conn.execute(state_meta.insert().values(
            key=resources.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
            value_json=json.dumps({"state": "pending", "instance_id": "old-instance"}),
            updated_at="before",
        ))
    monkeypatch.setattr(resources, "_configured_resource_state", lambda: resources.ConfiguredResourceState(
        status=resources.RESOURCE_BINDING_STATE_UNPAIRED, instance_id=None, instance_kind=None, runtime_ready=False,
    ))
    # Simulate a fresh CLI process, not the same-process bootstrap cache.
    monkeypatch.setattr(importer, "_ensured_targets", {})
    count_rows = importer._current_counts
    peer_commits = []

    def count_after_an_independent_write(conn):
        with engine.begin() as peer:
            peer.exec_driver_sql("PRAGMA busy_timeout = 0")
            peer.execute(state_meta.insert().values(key="count-probe", value_json="{}", updated_at="after"))
            sealed = peer.execute(select(state_meta.c.value_json).where(
                state_meta.c.key == resources.LEGACY_DEFERRED_CONTEXT_MIGRATION_KEY,
            )).scalar_one()
            assert json.loads(sealed)["state"] == "sealed_unattributed"
        peer_commits.append(True)
        return count_rows(conn)

    monkeypatch.setattr(importer, "_current_counts", count_after_an_independent_write)
    try:
        report = importer.ensure_sqlite_state(db_path=db, state_dir=state_dir)
        assert not report.imported
        assert peer_commits == [True]
        assert report.counts["legacy_deferred_runs"] == 0
    finally:
        engine.dispose()
