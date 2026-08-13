from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from core.memory.clear_intent import (
    ClearIntent,
    ClearIntentStore,
    ClearIntentUnreadable,
    MARKER_RELATIVE_PATH,
)


def test_marker_write_is_atomic_and_round_trips(tmp_path: Path):
    store = ClearIntentStore(tmp_path)
    intent = ClearIntent.new(operator_ref="user-1", pre_epoch=4)

    store.write(intent)

    assert store.load() == intent
    assert not list((tmp_path / "state/memory").glob(".clear-intent.*.tmp"))


def test_marker_schema_rejects_non_uuid4_and_extra_fields(tmp_path: Path):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "operation_id": "operation/id",
        "operator_ref": "user-1",
        "pre_epoch": 1,
        "target_epoch": 2,
        "state": "deleting",
        "error_code": None,
        "created_at": "now",
        "updated_at": "now",
        "extra": True,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()


@pytest.mark.parametrize("schema_version", (True, 1.0))
def test_marker_schema_rejects_non_integer_versions(tmp_path: Path, schema_version: object):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    intent = ClearIntent.new(operator_ref="user-1", pre_epoch=1)
    payload = {
        "schema_version": schema_version,
        "operation_id": intent.operation_id,
        "operator_ref": intent.operator_ref,
        "pre_epoch": intent.pre_epoch,
        "target_epoch": intent.target_epoch,
        "state": intent.state,
        "error_code": intent.error_code,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()


def test_marker_reader_rejects_oversized_regular_file(tmp_path: Path):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"{" + b"x" * (16 * 1024) + b"}")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()


def test_legacy_open_journal_migrates_then_removes_journal(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    os.chmod(journal.parent, 0o700)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    operation_id = "2f7e31f4-ecf6-4c11-a3ed-e6e4e30e5b0f"
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        (operation_id, "user-1", 2, 3, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    intent = ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert intent is not None
    assert intent.operation_id == operation_id
    assert intent.target_epoch == 3
    assert ClearIntentStore(tmp_path).load() == intent
    assert not journal.exists()


def test_legacy_operation_token_is_accepted_by_new_marker_reader(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    legacy_id = "a" * 32
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        (legacy_id, "user-1", 2, 3, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    store = ClearIntentStore(tmp_path)
    intent = store.migrate_legacy(current_epoch=2)

    assert intent is not None
    assert intent.operation_id == legacy_id
    assert store.load() == intent


def test_legacy_abort_resolution_migrates_to_a_fenced_failed_intent(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, resolution TEXT, "
        "started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        ("legacy-abort", "user-1", 2, 3, "recovery_needed", "abort", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    intent = ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert intent is not None
    assert intent.state == "failed"
    assert intent.error_code == "memory_clear_legacy_abort_unsupported"


@pytest.mark.parametrize(
    ("state", "resolution"),
    (("unknown", None), ("deleting", "abort"), ("recovery_needed", "unknown")),
)
def test_legacy_journal_rejects_invalid_state_and_resolution(
    tmp_path: Path, state: str, resolution: str | None
):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, resolution TEXT, "
        "started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        ("legacy-invalid", "user-1", 2, 3, state, resolution, "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert not (tmp_path / MARKER_RELATIVE_PATH).exists()
    assert journal.exists()


def test_legacy_open_journal_without_target_epoch_defers_until_store_epoch(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    operation_id = "2f7e31f4-ecf6-4c11-a3ed-e6e4e30e5b0f"
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, 1)",
        (operation_id, "user-1", 2, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    store = ClearIntentStore(tmp_path)
    assert store.migrate_legacy(current_epoch=None) is None
    assert journal.exists()

    intent = store.migrate_legacy(current_epoch=3)
    assert intent is not None
    assert intent.pre_epoch == 2
    assert intent.target_epoch == 3
    assert not journal.exists()


def test_legacy_journal_symlink_is_unreadable(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    target = tmp_path / "outside.sqlite"
    target.touch()
    journal.symlink_to(target)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)


def test_dangling_legacy_journal_is_not_treated_as_absent(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.symlink_to(tmp_path / "missing-clear-journal.sqlite")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)
