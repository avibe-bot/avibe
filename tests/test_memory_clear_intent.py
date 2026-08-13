from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from core.memory.clear_intent import (
    ClearIntent,
    ClearIntentError,
    ClearIntentStore,
    ClearIntentUnreadable,
    DEFAULT_CLEAR_SURFACES,
    MARKER_RELATIVE_PATH,
)


def _add_surface_receipts(connection: sqlite3.Connection, operation_id: str, state: str = "snapshotted") -> None:
    connection.execute(
        """
        CREATE TABLE clear_surface (
            operation_id TEXT,
            surface TEXT,
            relative_path TEXT,
            relative_snapshot_path TEXT,
            present INTEGER,
            pre_clear_digest TEXT,
            snapshot_digest TEXT,
            state TEXT,
            updated_at TEXT
        )
        """
    )
    for surface in ("queue", "provider", "call_log", "attachments"):
        path = next(item.relative_path for item in DEFAULT_CLEAR_SURFACES if item.surface == surface)
        connection.execute(
            "INSERT INTO clear_surface VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (operation_id, surface, path, f"state/memory/clear-snapshots/{operation_id}/{surface}", 0, None, None, state, "now"),
        )


def _add_recovery_from_column(connection: sqlite3.Connection, operation_id: str, state: str) -> None:
    connection.execute("ALTER TABLE clear_operation ADD COLUMN recovery_from_state TEXT")
    connection.execute(
        "UPDATE clear_operation SET recovery_from_state = ? WHERE operation_id = ?",
        (state, operation_id),
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


@pytest.mark.parametrize(
    ("state", "error_code"),
    (
        ("deleting", "memory_clear_failed"),
        ("deleting", "memory_clear_legacy_abort_unsupported"),
        ("failed", None),
    ),
)
def test_marker_schema_rejects_contradictory_state_and_error(
    tmp_path: Path, state: str, error_code: str
):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    intent = ClearIntent.new(operator_ref="user-1", pre_epoch=1)
    payload = {
        "schema_version": 1,
        "operation_id": intent.operation_id,
        "operator_ref": intent.operator_ref,
        "pre_epoch": intent.pre_epoch,
        "target_epoch": intent.target_epoch,
        "state": state,
        "error_code": error_code,
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
    _add_surface_receipts(connection, operation_id)
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    intent = ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert intent is not None
    assert intent.operation_id == operation_id
    assert intent.target_epoch == 3
    assert ClearIntentStore(tmp_path).load() == intent
    assert not journal.exists()


def test_legacy_open_journal_rejects_epoch_outside_replay_window(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
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
    _add_surface_receipts(connection, operation_id)
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=4)

    assert journal.exists()
    assert not (tmp_path / MARKER_RELATIVE_PATH).exists()


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
    _add_surface_receipts(connection, legacy_id)
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    store = ClearIntentStore(tmp_path)
    intent = store.migrate_legacy(current_epoch=2)

    assert intent is not None
    assert intent.operation_id == legacy_id
    assert store.load() == intent


def test_legacy_journal_without_started_at_is_unreadable(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", 2, 3, "deleting"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert journal.exists()


@pytest.mark.parametrize("column, value", (("operation_id", None), ("operator_ref", None)))
def test_legacy_identifiers_must_remain_released_strings(
    tmp_path: Path, column: str, value: object
):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id, operator_ref, pre_epoch INTEGER, "
        "target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    values = ["legacy-one", "user-1", 0, 1, "deleting", "2026-08-13T00:00:00Z", 1]
    values[0 if column == "operation_id" else 1] = value
    connection.execute("INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, ?)", values)
    connection.commit()
    connection.close()

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)
    assert journal.exists()


def test_legacy_open_operation_requires_all_surface_receipts(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, pre_epoch INTEGER, "
        "target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", 0, 1, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.execute(
        "CREATE TABLE clear_surface (operation_id TEXT, surface TEXT, relative_path TEXT, state TEXT)"
    )
    connection.execute(
        "INSERT INTO clear_surface VALUES (?, ?, ?, ?)",
        ("legacy-one", "queue", "state/memory/memory.sqlite", "deleted"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)
    assert journal.exists()


def test_legacy_terminal_operation_requires_consistent_surface_receipts(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, pre_epoch INTEGER, "
        "target_epoch INTEGER, state TEXT, resolution TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        ("legacy-one", "user-1", 0, 1, "completed", None, "2026-08-13T00:00:00Z"),
    )
    _add_surface_receipts(connection, "legacy-one", state="deleted")
    connection.execute(
        "UPDATE clear_surface SET state = 'pending' WHERE operation_id = ? AND surface = 'queue'",
        ("legacy-one",),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)
    assert journal.exists()


def test_legacy_journal_bounds_rows_and_field_sizes(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, pre_epoch INTEGER, "
        "target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("legacy-one", "x" * 5000, 0, 1, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=0)
    assert journal.exists()


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
    _add_surface_receipts(connection, "legacy-abort")
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
    _add_surface_receipts(connection, operation_id)
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


def test_legacy_journal_rejects_multiple_open_operations(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.executemany(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        (
            ("legacy-one", "user-1", 2, 3, "deleting", "2026-08-13T00:00:00Z"),
            ("legacy-two", "user-2", 2, 3, "recovery_needed", "2026-08-13T00:00:01Z"),
        ),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert journal.exists()


def test_legacy_journal_rejects_nonterminal_row_without_open_slot(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("legacy-one", "user-1", 2, 3, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert journal.exists()


def test_marker_disappearance_during_open_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from core.memory import clear_intent

    store = ClearIntentStore(tmp_path)
    store.write(ClearIntent.new(operator_ref="user-1", pre_epoch=1))
    original_open = clear_intent.open_confined_regular_file

    def unlink_before_open(home, path):
        path.unlink()
        return original_open(home, path)

    monkeypatch.setattr(clear_intent, "open_confined_regular_file", unlink_before_open)

    assert store.load() is None


@pytest.mark.parametrize(
    ("pre_epoch", "target_epoch"),
    ((1.5, 2.5), ("1", 2), (1, 2.5)),
)
def test_legacy_journal_rejects_coercible_epochs(
    tmp_path: Path, pre_epoch: object, target_epoch: object
):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch, target_epoch, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", pre_epoch, target_epoch, "deleting", "2026-08-13T00:00:00Z"),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=1)

    assert journal.exists()


def test_legacy_migration_requires_durable_journal_removal(tmp_path: Path, monkeypatch):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id TEXT, operator_ref TEXT, "
        "pre_epoch INTEGER, target_epoch INTEGER, state TEXT, started_at TEXT, open_slot INTEGER)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("legacy-one", "user-1", 2, 3, "deleting", "2026-08-13T00:00:00Z"),
    )
    _add_surface_receipts(connection, "legacy-one")
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)

    from core.memory import clear_intent

    original_remove = clear_intent.remove_confined_path

    def remove_then_fail(home, path):
        if path == journal:
            journal.unlink()
            raise clear_intent.ConfinedFilesystemError("journal fsync failed")
        return original_remove(home, path)

    monkeypatch.setattr(clear_intent, "remove_confined_path", remove_then_fail)
    with pytest.raises(ClearIntentError):
        ClearIntentStore(tmp_path).migrate_legacy(current_epoch=2)

    assert ClearIntentStore(tmp_path).load() is not None


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
