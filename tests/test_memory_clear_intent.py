from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from core.memory.clear_intent import (
    LEGACY_RERUN_ERROR_CODE,
    MARKER_RELATIVE_PATH,
    ClearIntent,
    ClearIntentStore,
    ClearIntentUnreadable,
    cleanup_legacy_backup_storage,
    cleanup_legacy_provider_call_storage,
)
from core.memory.confined_filesystem import ConfinedFilesystemError


def _write_legacy_journal(home: Path, *, open_slot: object = None) -> Path:
    journal = home / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id, operator_ref, target_epoch, "
        "state, resolution, open_slot)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?)",
        (b"uninterpretable", None, "not-an-epoch", "foreign", {"bad": "type"}.__str__(), open_slot),
    )
    connection.commit()
    connection.close()
    os.chmod(journal, 0o600)
    return journal


def test_marker_write_is_atomic_and_round_trips(tmp_path: Path):
    store = ClearIntentStore(tmp_path)
    intent = ClearIntent.new(operator_ref="user-1", pre_epoch=4)

    store.write(intent)

    assert store.load() == intent
    assert not list((tmp_path / "state/memory").glob(".clear-intent.*.tmp"))


def test_marker_schema_rejects_non_uuid4_and_extra_fields(tmp_path: Path):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "legacy-operation",
                "operator_ref": "user-1",
                "pre_epoch": 1,
                "target_epoch": 2,
                "state": "deleting",
                "error_code": None,
                "created_at": "now",
                "updated_at": "now",
                "extra": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()


@pytest.mark.parametrize("schema_version", (True, 1.0))
def test_marker_schema_rejects_non_integer_versions(tmp_path: Path, schema_version: object):
    store = ClearIntentStore(tmp_path)
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
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ClearIntentUnreadable):
        store.load()


@pytest.mark.parametrize(
    ("state", "error_code"),
    (("deleting", "memory_clear_failed"), ("failed", None)),
)
def test_marker_schema_rejects_contradictory_state_and_error(
    tmp_path: Path, state: str, error_code: str | None
):
    store = ClearIntentStore(tmp_path)
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
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ClearIntentUnreadable):
        store.load()


def test_marker_reader_rejects_oversized_regular_file(tmp_path: Path):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"{" + b"x" * (16 * 1024) + b"}")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()


def test_legacy_terminal_journal_is_removed_without_marker(tmp_path: Path):
    journal = _write_legacy_journal(tmp_path)

    intent = ClearIntentStore(tmp_path).reconcile_legacy(current_epoch=7)

    assert intent is None
    assert not journal.exists()
    assert ClearIntentStore(tmp_path).load() is None


def test_legacy_open_probe_ignores_semantics_and_requires_explicit_rerun(tmp_path: Path):
    journal = _write_legacy_journal(tmp_path, open_slot="foreign-open-token")

    intent = ClearIntentStore(tmp_path).reconcile_legacy(current_epoch=7)

    assert intent is not None
    assert uuid.UUID(intent.operation_id).version == 4
    assert intent.operator_ref == "legacy-clear-journal"
    assert intent.pre_epoch == 7
    assert intent.target_epoch == 8
    assert intent.state == "failed"
    assert intent.error_code == LEGACY_RERUN_ERROR_CODE
    assert not journal.exists()


def test_legacy_probe_failure_requires_explicit_rerun(tmp_path: Path):
    journal = tmp_path / "state/memory/clear-journal.sqlite"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not sqlite")

    intent = ClearIntentStore(tmp_path).reconcile_legacy(current_epoch=2)

    assert intent is not None
    assert intent.error_code == LEGACY_RERUN_ERROR_CODE
    assert not journal.exists()


def test_existing_marker_wins_over_legacy_journal(tmp_path: Path):
    store = ClearIntentStore(tmp_path)
    existing = ClearIntent.new(operator_ref="current", pre_epoch=3)
    store.write(existing)
    journal = _write_legacy_journal(tmp_path, open_slot=1)

    assert store.reconcile_legacy(current_epoch=99) == existing
    assert store.load() == existing
    assert not journal.exists()


def test_legacy_journal_cleanup_failure_does_not_replace_failed_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _write_legacy_journal(tmp_path, open_slot=1)
    from core.memory import clear_intent

    original_remove = clear_intent.remove_confined_path

    def fail_journal_remove(home: Path, path: Path) -> None:
        if path == journal:
            raise clear_intent.ConfinedFilesystemError("busy")
        original_remove(home, path)

    monkeypatch.setattr(clear_intent, "remove_confined_path", fail_journal_remove)

    intent = ClearIntentStore(tmp_path).reconcile_legacy(current_epoch=4)

    assert intent is not None
    assert ClearIntentStore(tmp_path).load() == intent
    assert journal.exists()


def test_legacy_residue_cleanup_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backup_journal = tmp_path / "state/memory/backup-restore-journal.sqlite"
    backup_file = tmp_path / "state/memory/backups/old/data"
    snapshot = tmp_path / "state/memory/clear-snapshots/old/data"
    for path in (backup_journal, backup_file, snapshot):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("obsolete", encoding="utf-8")
    from core.memory import clear_intent

    original_remove = clear_intent.remove_confined_path

    def fail_one(home: Path, path: Path) -> None:
        if path == backup_journal:
            raise clear_intent.ConfinedFilesystemError("busy")
        original_remove(home, path)

    monkeypatch.setattr(clear_intent, "remove_confined_path", fail_one)

    assert cleanup_legacy_backup_storage(tmp_path) == (
        "state/memory/backup-restore-journal.sqlite",
    )
    assert backup_journal.exists()
    assert not backup_file.exists()
    assert not snapshot.exists()


def test_legacy_provider_call_cleanup_removes_files_without_opening_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_root = tmp_path / "memory" / "call-log"
    legacy_root.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        (legacy_root / f"call-log.db{suffix}").write_bytes(b"retired")
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("legacy cleanup opened SQLite"),
    )

    cleanup_legacy_provider_call_storage(tmp_path)

    assert not legacy_root.exists()
    assert not (tmp_path / MARKER_RELATIVE_PATH).exists()


def test_legacy_provider_call_cleanup_does_not_follow_directory_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    survivor = outside / "survivor"
    survivor.write_text("keep", encoding="utf-8")
    legacy_root = tmp_path / "memory" / "call-log"
    legacy_root.parent.mkdir(parents=True)
    legacy_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfinedFilesystemError):
        cleanup_legacy_provider_call_storage(tmp_path)

    assert legacy_root.is_symlink()
    assert survivor.read_text(encoding="utf-8") == "keep"


def test_legacy_provider_call_cleanup_rejects_foreign_root_file(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "memory" / "call-log"
    legacy_root.parent.mkdir(parents=True)
    legacy_root.write_bytes(b"foreign")

    with pytest.raises(ConfinedFilesystemError):
        cleanup_legacy_provider_call_storage(tmp_path)

    assert legacy_root.read_bytes() == b"foreign"


def test_legacy_provider_call_cleanup_preserves_foreign_directory_contents(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "memory" / "call-log"
    legacy_root.mkdir(parents=True)
    (legacy_root / "call-log.db").write_bytes(b"retired")
    foreign = legacy_root / "foreign.txt"
    foreign.write_bytes(b"foreign")

    with pytest.raises(ConfinedFilesystemError):
        cleanup_legacy_provider_call_storage(tmp_path)

    assert foreign.read_bytes() == b"foreign"


def test_legacy_provider_call_cleanup_rejects_known_name_symlink(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"foreign")
    legacy_root = tmp_path / "memory" / "call-log"
    legacy_root.mkdir(parents=True)
    legacy_file = legacy_root / "call-log.db"
    legacy_file.symlink_to(outside)

    with pytest.raises(ConfinedFilesystemError):
        cleanup_legacy_provider_call_storage(tmp_path)

    assert legacy_file.is_symlink()
    assert outside.read_bytes() == b"foreign"


def test_marker_disappearance_during_open_is_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from core.memory import clear_intent

    store = ClearIntentStore(tmp_path)
    store.write(ClearIntent.new(operator_ref="user-1", pre_epoch=1))
    original_open = clear_intent.open_confined_regular_file

    def unlink_before_open(home: Path, path: Path) -> int:
        path.unlink()
        return original_open(home, path)

    monkeypatch.setattr(clear_intent, "open_confined_regular_file", unlink_before_open)

    assert store.load() is None


def test_dangling_marker_is_fail_closed(tmp_path: Path):
    marker = tmp_path / MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.symlink_to(tmp_path / "missing-marker")

    with pytest.raises(ClearIntentUnreadable):
        ClearIntentStore(tmp_path).load()
