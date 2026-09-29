from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from storage import backups
from storage.importer import ensure_sqlite_state
from storage.lock import MigrationFileLock, migration_lock_path_for
from storage.migrations import run_migrations


START = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
READY = START + timedelta(seconds=20)
EXPIRES = READY + timedelta(hours=72)


def _database(tmp_path: Path) -> Path:
    target = tmp_path / "donn\u00e9es" / "vibe.sqlite"
    target.parent.mkdir()
    with sqlite3.connect(target) as connection:
        connection.execute("create table alembic_version (version_num text primary key)")
        connection.execute("insert into alembic_version values ('old')")
        connection.execute("create table notes (body text)")
        connection.execute("insert into notes values (?)", ("caf\u00e9",))
    return target


def _attempt(db_path: Path, *, phase: str = "ready", now: datetime = START, revision: str = "new") -> Path:
    backup = backups.create_sqlite_migration_backup(db_path, to_revisions={revision}, now=now)
    attempt = backups.begin_sqlite_backup_migration(db_path, backup)
    if phase == "migrating":
        return backup
    with sqlite3.connect(db_path) as connection:
        connection.execute("update alembic_version set version_num = ?", (revision,))
    backups.complete_sqlite_backup_migration(db_path, attempt, now=now)
    if phase == "migrated":
        return backup
    assert backups.begin_sqlite_backup_validation(db_path) == attempt
    if phase == "validating":
        return backup
    backups.complete_sqlite_backup_validation(db_path, attempt, now=now + timedelta(seconds=10))
    if phase == "validated":
        return backup
    assert backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=attempt, expected_revisions={revision}, now=now + timedelta(seconds=20)
    )
    return backup


def _manifest(backup: Path) -> dict:
    return json.loads((backup / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(backup: Path, manifest: dict) -> None:
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("moment", "deleted"),
    [
        (EXPIRES - timedelta(microseconds=1), False),
        (EXPIRES, True),
        (EXPIRES + timedelta(days=10), True),
        (EXPIRES.astimezone(timezone(timedelta(hours=8))), True),
    ],
)
def test_expiry_boundary_can_remove_the_last_backup(tmp_path, moment, deleted):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)

    result = backups.expire_sqlite_migration_backups(db_path, now=moment)

    assert backup.exists() is not deleted
    assert result["removed"] == ([backup.name] if deleted else [])
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select body from notes").fetchall() == [("caf\u00e9",)]
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=30))["removed"] == (
        [backup.name] if not deleted else []
    )


def test_restart_does_not_reset_ready_time(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    before = _manifest(backup)

    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=before["expiry_lifecycle"]["attempt_id"],
        expected_revisions={"new"}, now=READY + timedelta(days=2)
    )
    assert _manifest(backup) == before
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["removed"] == [backup.name]


def test_count_limit_and_age_expiry_apply_independently(tmp_path):
    db_path = _database(tmp_path)
    copies = [
        _attempt(db_path, now=START + timedelta(hours=index), revision=f"new-{index}")
        for index in range(backups.SQLITE_BACKUP_RETENTION + 2)
    ]
    surviving = [backup for backup in copies if backup.exists()]
    assert surviving == copies[-backups.SQLITE_BACKUP_RETENTION:]
    assert backups.expire_sqlite_migration_backups(db_path, now=READY + timedelta(hours=5))["removed"] == []
    result = backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(hours=5))
    assert set(result["removed"]) == {backup.name for backup in surviving}
    assert not any(backup.exists() for backup in copies)


@pytest.mark.parametrize("phase", ["migrating", "migrated", "validating", "validated"])
def test_unconfirmed_attempt_never_expires_by_age(tmp_path, phase):
    db_path = _database(tmp_path)
    backup = _attempt(db_path, phase=phase)

    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=90)) == {
        "status": "unconfirmed",
        "removed": [],
    }
    assert backup.exists()


def test_service_with_unrelated_target_cannot_confirm_attempt(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path, phase="validated")
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=_manifest(backup)["expiry_lifecycle"]["attempt_id"],
        expected_revisions={"unrelated"}, now=READY
    )
    assert _manifest(backup)["expiry_lifecycle"]["phase"] == "validated"


def test_readiness_cannot_adopt_a_later_attempt_with_the_same_revisions(tmp_path):
    db_path = _database(tmp_path)
    earlier = _attempt(db_path, phase="validated")
    previous_attempt = _manifest(earlier)["expiry_lifecycle"]["attempt_id"]
    later = _attempt(db_path, phase="validated", now=START + timedelta(days=1))

    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=previous_attempt, expected_revisions={"new"}
    )
    assert _manifest(later)["expiry_lifecycle"]["phase"] == "validated"


def test_new_unconfirmed_attempt_fences_older_expired_backups(tmp_path):
    db_path = _database(tmp_path)
    old = _attempt(db_path)
    newer = backups.create_sqlite_migration_backup(
        db_path, to_revisions={"future"}, now=READY + timedelta(days=4)
    )
    backups.begin_sqlite_backup_migration(db_path, newer)
    # Simulate a migration that committed partial data, but did not advance its stamp.
    with sqlite3.connect(db_path) as connection:
        connection.execute("insert into notes values ('partial change')")

    result = backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=1))

    assert result["status"] == "unconfirmed"
    assert old.exists() and newer.exists()
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=_manifest(old)["expiry_lifecycle"]["attempt_id"],
        expected_revisions={"new"}, now=EXPIRES
    )


def test_failed_metadata_publication_cannot_leave_old_expiry_authority(monkeypatch, tmp_path):
    db_path = _database(tmp_path)
    old = _attempt(db_path)
    newer = backups.create_sqlite_migration_backup(db_path, to_revisions={"future"})
    real_write = backups._write_backup_metadata

    def fail_manifest(path, payload):
        if path == newer / "manifest.json":
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(backups, "_write_backup_metadata", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        backups.begin_sqlite_backup_migration(db_path, newer)
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "unconfirmed"
    assert old.exists()


def test_partial_readiness_receipt_is_not_repaired_by_guessing(monkeypatch, tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path, phase="validated")
    real_write = backups._write_backup_metadata

    def fail_record(path, payload):
        if path == backups._lifecycle_path(db_path):
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(backups, "_write_backup_metadata", fail_record)
    with pytest.raises(OSError, match="disk full"):
        backups.confirm_sqlite_backup_readiness(
            db_path, attempt_id=_manifest(backup)["expiry_lifecycle"]["attempt_id"],
            expected_revisions={"new"}, now=READY
        )
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "unconfirmed"
    assert backup.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("attempt_id", "invalid"),
        ("phase", "restored"),
        ("phase", []),
        ("phase", {}),
        ("database", "/unrelated/vibe.sqlite"),
        ("database_identity", [False, True]),
        ("backup_name", "../another"),
        ("migration_completed_at", None),
        ("migration_completed_at", "not-a-date"),
        ("validated_at", "2026-09-28T11:59:00+00:00"),
        ("ready_at", "2026-09-28T12:00:20"),
        ("ready_at", "2099-01-01T00:00:00Z"),
        ("ready_at", "2026-09-28T12:00:00Z"),
    ],
)
def test_malformed_expiry_evidence_is_not_deletion_authority(tmp_path, field, value):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    manifest = _manifest(backup)
    manifest["expiry_lifecycle"][field] = value
    _write_manifest(backup, manifest)

    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["removed"] == []
    assert backup.exists()


@pytest.mark.parametrize("payload", [b"\xff", b'{"phase": []}', b'{"phase": {}}'])
def test_unreadable_or_malformed_sidecar_does_not_block_database_use(tmp_path, payload):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    backups._lifecycle_path(db_path).write_bytes(payload)

    assert backups.begin_sqlite_backup_validation(db_path) is None
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=_manifest(backup)["expiry_lifecycle"]["attempt_id"], expected_revisions={"new"}
    )
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "unconfirmed"
    assert backup.exists()


def test_unknown_legacy_json_and_manual_files_are_not_age_expired(tmp_path):
    db_path = _database(tmp_path)
    ready = _attempt(db_path)
    legacy = backups.create_sqlite_migration_backup(db_path, now=START - timedelta(days=300))
    root = ready.parent
    manual = root / "manual-keep.sqlite"
    manual.write_text("user data", encoding="utf-8")
    json_backup = root / "sqlite-state-migration-20260101T000000Z"
    json_backup.mkdir()
    (json_backup / "manifest.json").write_text(
        json.dumps({"created_at": "2026-01-01T00:00:00Z", "files": {}}), encoding="utf-8"
    )

    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["removed"] == [ready.name]
    assert legacy.exists()
    assert manual.read_text(encoding="utf-8") == "user data"
    assert json_backup.exists()


@pytest.mark.parametrize("extra_name", [backups.REPLACED_DATABASE_NAME, "operator-notes.txt"])
def test_user_added_or_displaced_content_is_not_age_expired(tmp_path, extra_name):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    extra = backup / extra_name
    extra.write_text("not temporary", encoding="utf-8")
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["removed"] == []
    assert extra.read_text(encoding="utf-8") == "not temporary"


def test_symlink_backup_root_is_not_traversed(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    actual_root = tmp_path / "external"
    backup.parent.rename(actual_root)
    backup.parent.symlink_to(actual_root, target_is_directory=True)
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["removed"] == []
    assert (actual_root / backup.name).exists()


def test_restore_revokes_all_prior_expiry_and_preserves_displaced_writes(tmp_path):
    db_path = _database(tmp_path)
    old = _attempt(db_path)
    latest = _attempt(db_path, now=START + timedelta(days=1))
    with sqlite3.connect(db_path) as connection:
        connection.execute("insert into notes values ('after backup')")

    displaced = backups.restore_sqlite_backup(old, db_path)

    assert displaced is not None
    with sqlite3.connect(displaced) as connection:
        assert connection.execute("select body from notes where body = 'after backup'").fetchone()
    assert _manifest(old)["expiry_lifecycle"]["phase"] == "restored"
    assert _manifest(latest)["expiry_lifecycle"]["phase"] == "restored"
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=10))["removed"] == []
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=_manifest(old)["expiry_lifecycle"]["attempt_id"], expected_revisions={"old"}
    )


def test_failed_restore_does_not_keep_an_old_expiry_clock(monkeypatch, tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)

    def fail_swap(*args, **kwargs):
        raise OSError("restore failed")

    monkeypatch.setattr(backups, "_swap_live_database", fail_swap)
    with pytest.raises(OSError, match="restore failed"):
        backups.restore_sqlite_backup(backup, db_path)
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "unconfirmed"
    assert backup.exists()


def test_expiry_defers_when_migration_lock_is_owned_by_another_thread(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with MigrationFileLock(migration_lock_path_for(db_path), timeout_seconds=None):
            acquired.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    try:
        assert acquired.wait(10)
        assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "busy"
        assert backup.exists()
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def test_replaced_database_cannot_inherit_previous_expiry_receipts(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    replacement = db_path.with_name("replacement.sqlite")
    with sqlite3.connect(db_path) as source, sqlite3.connect(replacement) as target:
        source.backup(target)
    replacement.replace(db_path)

    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES)["status"] == "unconfirmed"
    assert backup.exists()


def test_cancelled_expiry_does_not_delete(tmp_path):
    db_path = _database(tmp_path)
    backup = _attempt(db_path)
    cancelled = threading.Event()
    cancelled.set()
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES, cancel_event=cancelled)["status"] == "cancelled"
    assert backup.exists()


def test_real_migration_import_and_service_readiness_enable_expiry(monkeypatch, tmp_path):
    from config import paths
    from vibe import runtime

    state_dir = tmp_path / "state"
    db_path = state_dir / "vibe.sqlite"
    state_dir.mkdir()
    run_migrations(db_path, revision="20260627_0025")
    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")
    record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
    backup = state_dir / "backups" / record["backup_name"]
    assert record["phase"] == "validated"
    assert backups.expire_sqlite_migration_backups(db_path)["status"] == "unconfirmed"
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)

    # The real runtime owner, not a synthetic "healthy" flag, confirms readiness.
    runtime.acquire_service_instance_lock()
    try:
        runtime.mark_service_instance_started()
        ready = datetime.fromisoformat(_manifest(backup)["expiry_lifecycle"]["ready_at"])
        runtime.mark_service_instance_started()
        assert datetime.fromisoformat(_manifest(backup)["expiry_lifecycle"]["ready_at"]) == ready
        assert backups.expire_sqlite_migration_backups(db_path, now=ready + timedelta(hours=72))["removed"] == [
            backup.name
        ]
    finally:
        runtime.release_service_instance_lock()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_busy_readiness_retries_on_maintenance_without_renewing_clock(monkeypatch, tmp_path):
    from config import paths
    from core.controller import Controller
    from vibe import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")
    assert report.backup_attempt_id
    record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
    backup = state_dir / "backups" / record["backup_name"]
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with MigrationFileLock(migration_lock_path_for(db_path), timeout_seconds=None):
            acquired.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold_lock)
    worker.start()
    runtime.acquire_service_instance_lock()
    try:
        assert acquired.wait(10)
        runtime.mark_service_instance_started()
        pending = runtime._PENDING_SQLITE_BACKUP_READINESS
        assert pending is not None
        original_ready_at = pending[-1]
        assert _manifest(backup)["expiry_lifecycle"]["phase"] == "validated"
        release.set()
        worker.join(10)
        real_expiry = backups.expire_sqlite_migration_backups
        observed = []

        def expiry_after_deadline(path, **kwargs):
            observed.append(_manifest(backup)["expiry_lifecycle"]["ready_at"])
            return real_expiry(path, now=original_ready_at + timedelta(hours=72), **kwargs)

        monkeypatch.setattr(backups, "expire_sqlite_migration_backups", expiry_after_deadline)
        controller = Controller.__new__(Controller)
        assert controller._run_sqlite_backup_expiry_pass()["removed"] == [backup.name]
        assert observed == [original_ready_at.isoformat()]
        assert runtime._PENDING_SQLITE_BACKUP_READINESS is None
    finally:
        release.set()
        worker.join(10)
        runtime.release_service_instance_lock()
    assert not worker.is_alive()


def test_service_cannot_adopt_another_process_validation(monkeypatch, tmp_path):
    from config import paths
    from storage.importer import reset_ensured_sqlite_state
    from vibe import runtime

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")
    reset_ensured_sqlite_state()
    monkeypatch.setattr(paths, "get_sqlite_state_path", lambda: db_path)
    runtime.acquire_service_instance_lock()
    try:
        runtime.mark_service_instance_started()
        record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
        assert record["phase"] == "validated"
        assert runtime._PENDING_SQLITE_BACKUP_READINESS is None
    finally:
        runtime.release_service_instance_lock()


@pytest.mark.parametrize("phase", ["migrated", "validating", "validated"])
@pytest.mark.parametrize("receipt", ["manifest", "sidecar"])
def test_optional_receipt_failure_retains_backup_without_failing_startup(monkeypatch, tmp_path, phase, receipt):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    real_write = backups._write_backup_metadata
    failures = []

    def fail_receipt(path, payload):
        lifecycle = payload.get("expiry_lifecycle", payload)
        is_manifest = path.name == "manifest.json"
        if lifecycle.get("phase") == phase and is_manifest == (receipt == "manifest"):
            failures.append(path)
            raise OSError("disk full while publishing optional receipt")
        real_write(path, payload)

    monkeypatch.setattr(backups, "_write_backup_metadata", fail_receipt)
    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")

    assert report.db_path == db_path
    assert failures
    record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
    backup = state_dir / "backups" / record["backup_name"]
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=record["attempt_id"], expected_revisions=record["to_revisions"]
    )
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=90))["removed"] == []
    assert backup.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_initial_fence_failure_aborts_before_sql_changes(monkeypatch, tmp_path):
    from storage import migrations

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    real_write = backups._write_backup_metadata

    def fail_fence(path, payload):
        if path == backups._lifecycle_path(db_path):
            raise OSError("cannot invalidate prior expiry authority")
        real_write(path, payload)

    def unexpected_upgrade(*args, **kwargs):
        pytest.fail("The database must not change before the blocking record is durable")

    monkeypatch.setattr(backups, "_write_backup_metadata", fail_fence)
    monkeypatch.setattr(migrations.command, "upgrade", unexpected_upgrade)
    with pytest.raises(OSError, match="cannot invalidate"):
        run_migrations(db_path)
    with sqlite3.connect(db_path) as connection:
        assert backups._stamped_revisions(connection) == ("20260627_0025",)


def test_relocated_backup_directory_keeps_upgrades_working_without_age_expiry(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")
    external = tmp_path / "external-backups"
    external.mkdir()
    (state_dir / "backups").symlink_to(external, target_is_directory=True)

    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")

    assert report.db_path == db_path
    copies = [entry for entry in external.iterdir() if entry.name.startswith("avibe-sqlite-migration-")]
    assert len(copies) == 1
    assert "expiry_lifecycle" not in _manifest(copies[0])
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=90))["removed"] == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_import_failure_keeps_migrated_backup_unconfirmed(monkeypatch, tmp_path):
    from storage import importer

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")

    def fail_import(*args, **kwargs):
        raise RuntimeError("data import failed")

    monkeypatch.setattr(importer, "_parse_json_state", fail_import)
    with pytest.raises(RuntimeError, match="data import failed"):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="avibe")
    record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
    assert record["phase"] == "validating"
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=30))["removed"] == []
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=record["attempt_id"], expected_revisions=record["to_revisions"]
    )


def test_real_failed_migration_cannot_gain_readiness_from_unchanged_revision(monkeypatch, tmp_path):
    from storage import migrations

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")

    def fail_upgrade(*args, **kwargs):
        with sqlite3.connect(db_path) as connection:
            connection.execute("create table partial_write (value text)")
        raise RuntimeError("partial migration")

    monkeypatch.setattr(migrations.command, "upgrade", fail_upgrade)
    with pytest.raises(RuntimeError, match="partial migration"):
        run_migrations(db_path)
    record = json.loads(backups._lifecycle_path(db_path).read_text(encoding="utf-8"))
    assert record["phase"] == "migrating"
    assert not backups.confirm_sqlite_backup_readiness(
        db_path, attempt_id=record["attempt_id"], expected_revisions=record["to_revisions"]
    )
    assert backups.expire_sqlite_migration_backups(db_path, now=EXPIRES + timedelta(days=30))["removed"] == []
