"""Focused identity-only Memory store contract tests."""

from pathlib import Path
import sqlite3

import pytest

from core.memory.store import MEMORY_STORE_SCHEMA_VERSION, MemoryStore


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "memory" / "memory.sqlite"


def test_new_store_is_identity_only_v4(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path), effective_home=tmp_path)
    store.ensure_meta()
    with sqlite3.connect(store.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }
        assert tables == {"memory_meta", "memory_projects"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MEMORY_STORE_SCHEMA_VERSION


def test_volatile_admission_preserves_identity_without_payload_tables(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path), effective_home=tmp_path)
    principal = store.principal_for_user_key("slack:U123")
    project = "project-slug"
    admission = store.admit_volatile_capture(
        source_message_id="source-1",
        session_id="session-1",
        principal_id=principal,
        project_ref=project,
        provenance="user_input",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert admission.outcome == "accepted"
    assert admission.provider_session_ref is not None
    with sqlite3.connect(store.path) as conn:
        assert not {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } & {"memory_queue", "memory_delivery", "memory_attachments"}
    store.mark_capture_success()
    assert store.has_provider_data_history()


def test_clear_preserves_scope_key_and_rotates_epoch(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path), effective_home=tmp_path)
    before = store.ensure_meta()
    store.reset_for_clear()
    after = store.ensure_meta()
    assert after.epoch == before.epoch + 1
    assert after.scope_key == before.scope_key
    assert after.last_success_at is None


def test_released_v2_migration_discards_delivery_tables_and_derives_projects(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    schema = Path("core/memory/schema_v2.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            """
            INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                last_provider_timestamp_ms, missed_count, last_success_at, last_error,
                last_error_at, processing_fault_generation, processing_fault_kind,
                processing_fault_since, processing_alert_active, updated_at
            ) VALUES (1, 0, 0, ?, 'root', 0, 0, NULL, NULL, NULL, 0, NULL, NULL, 0, ?)
            """,
            (b"k" * 32, "2026-01-01T00:00:00Z"),
        )
        meta = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        assert meta is not None
        principal = "u-" + "a" * 32
        project = "p-" + "b" * 32
        conn.execute(
            """
            INSERT INTO memory_capture_queue (
                source_message_digest, epoch, session_id, provider_session_ref, generation,
                principal_id, project_ref, provenance, payload_text, payload_attachments,
                attachment_bundle_id, occurred_at_ms, provider_timestamp_ms, state, attempts,
                next_retry_at, lease_owner, lease_at, lease_token, last_error, created_at, completed_at
            ) VALUES ('digest', 0, 'session', 'ref', 1, ?, ?, 'user_input',
                      'payload', NULL, NULL, 1, 1, 'pending', 0, NULL, NULL, NULL, 0, NULL,
                      '2026-01-01T00:00:00Z', NULL)
            """,
            (principal, project),
        )
    store = MemoryStore(path, effective_home=tmp_path)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT project_id FROM memory_projects WHERE principal_id = ?",
            (principal,),
        ).fetchone()[0] == project
    with sqlite3.connect(store.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }
        assert tables == {"memory_meta", "memory_projects"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


@pytest.mark.parametrize("version", [0, 1, 3])
def test_released_identity_shapes_migrate_without_provider_io(tmp_path: Path, version: int) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_meta (
                singleton INTEGER PRIMARY KEY,
                epoch INTEGER NOT NULL,
                clear_in_progress INTEGER NOT NULL,
                scope_key BLOB NOT NULL,
                provider_root_id TEXT NOT NULL,
                last_provider_timestamp_ms INTEGER NOT NULL,
                missed_count INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_error TEXT,
                last_error_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE memory_capture_queue (
                source_message_digest TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                project_ref TEXT NOT NULL,
                created_at TEXT
            );
            """
        )
        if version == 3:
            conn.execute(
                "CREATE TABLE memory_projects (principal_id TEXT, project_id TEXT)"
            )
        principal = "u-" + "c" * 32
        project = "p-" + ("d" * 32)
        conn.execute(
            "INSERT INTO memory_meta VALUES (1, 7, 0, ?, 'legacy-root', 42, 3, ?, NULL, NULL, ?)",
            (b"z" * 32, "2026-02-03T00:00:00Z", "2026-02-03T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO memory_capture_queue VALUES ('digest', ?, ?, ?)",
            (principal, project, "2026-02-01T00:00:00Z"),
        )
        if version == 3:
            conn.execute(
                "INSERT INTO memory_projects VALUES (?, ?)",
                (principal, project),
            )
        conn.execute(f"PRAGMA user_version = {version}")
    store = MemoryStore(path, effective_home=tmp_path)
    meta = store.ensure_meta()
    assert (meta.epoch, meta.scope_key, meta.provider_root_id) == (7, b"z" * 32, "legacy-root")
    assert meta.last_provider_timestamp_ms == 42
    assert meta.last_success_at == "2026-02-03T00:00:00Z"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT project_id FROM memory_projects WHERE principal_id = ?", (principal,)).fetchone()[0] == project
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
