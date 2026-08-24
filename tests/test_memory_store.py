"""Focused identity-only Memory store contract tests."""

import hashlib
import hmac
from pathlib import Path
import sqlite3

import pytest

from core.memory.project_ids import MAX_NAMED_MEMORY_PROJECTS
from core.memory.store import (
    MEMORY_STORE_SCHEMA_VERSION,
    MemoryStore,
    _provider_session_ref,
)


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


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("memory_meta", "updated_at"),
        ("memory_projects", "last_written_at"),
    ],
)
def test_incomplete_v4_store_is_rejected_during_initialization(
    tmp_path: Path,
    table: str,
    column: str,
) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("core/memory/schema.sql").read_text(encoding="utf-8"))
        conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        conn.execute(f"PRAGMA user_version = {MEMORY_STORE_SCHEMA_VERSION}")

    with pytest.raises(RuntimeError, match="schema is incomplete"):
        MemoryStore(path, effective_home=tmp_path)


@pytest.mark.parametrize("table", ["memory_meta", "memory_projects"])
def test_v4_store_without_required_primary_key_is_rejected_during_initialization(
    tmp_path: Path,
    table: str,
) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    replacement = f"{table}_without_key"
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("core/memory/schema.sql").read_text(encoding="utf-8"))
        conn.execute(f'CREATE TABLE "{replacement}" AS SELECT * FROM "{table}"')
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')
        conn.execute(f"PRAGMA user_version = {MEMORY_STORE_SCHEMA_VERSION}")

    with pytest.raises(RuntimeError, match="schema is incomplete"):
        MemoryStore(path, effective_home=tmp_path)


def test_store_rejects_symlinked_database_without_touching_target(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "memory" / "memory.sqlite"
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside.sqlite"
    with sqlite3.connect(outside) as conn:
        conn.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        conn.execute("INSERT INTO evidence VALUES ('keep-me')")
    before = outside.read_bytes()
    path.symlink_to(outside)

    with pytest.raises(OSError, match="private regular file"):
        MemoryStore(path, effective_home=home)

    assert outside.read_bytes() == before
    with sqlite3.connect(outside) as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone()[0] == "keep-me"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_store_rejects_symlinked_sqlite_sidecars_before_initialization(
    tmp_path: Path,
    suffix: str,
) -> None:
    home = tmp_path / "home"
    path = home / "state" / "memory" / "memory.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path):
        pass
    outside = tmp_path / "outside-sidecar"
    outside.write_bytes(b"keep-me")
    before = outside.read_bytes()
    path.with_name(f"{path.name}{suffix}").symlink_to(outside)

    with pytest.raises(OSError, match="private regular file"):
        MemoryStore(path, effective_home=home)

    assert outside.read_bytes() == before


def test_volatile_admission_preserves_identity_without_payload_tables(tmp_path: Path) -> None:
    """MEMORY-SEARCH-013: admission persists identity but no delivery payload."""

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


def test_data_loss_settlement_preserves_stable_identity_and_rotates_epoch(
    tmp_path: Path,
) -> None:
    """MEMORY-REPAIR-302: destructive reset preserves stable identity."""

    store = MemoryStore(_store_path(tmp_path), effective_home=tmp_path)
    before = store.ensure_meta()
    principal = store.principal_for_user_key("slack:U123")
    store.admit_volatile_capture(
        source_message_id="source-1",
        session_id="session-1",
        principal_id=principal,
        project_ref="project-slug",
        provenance="user_input",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )

    store.settle_after_data_loss()

    after = store.ensure_meta()
    assert after.epoch == before.epoch + 1
    assert after.scope_key == before.scope_key
    assert after.provider_root_id == before.provider_root_id
    assert after.last_success_at is None
    assert store.list_memory_projects(principal) == ("default", "project-slug")


def test_released_v2_migration_discards_delivery_tables_and_derives_projects(tmp_path: Path) -> None:
    """MEMORY-SEARCH-005: released delivery stores migrate to identity-only v4."""

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
            ) VALUES (1, 0, 0, ?, 'root', 42, 0, ?, NULL, NULL, 0, NULL, NULL, 0, ?)
            """,
            (b"k" * 32, "2026-02-03T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        meta = conn.execute("SELECT * FROM memory_meta WHERE singleton = 1").fetchone()
        assert meta is not None
        principal = "u-" + "a" * 32
        legacy_projects = tuple(
            f"p-{index:032x}" for index in range(MAX_NAMED_MEMORY_PROJECTS)
        )
        for index, project in enumerate(legacy_projects):
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, provider_session_ref, generation,
                    principal_id, project_ref, provenance, payload_text, payload_attachments,
                    attachment_bundle_id, occurred_at_ms, provider_timestamp_ms, state, attempts,
                    next_retry_at, lease_owner, lease_at, lease_token, last_error, created_at, completed_at
                ) VALUES (?, 0, 'session', ?, 1, ?, ?, 'user_input',
                          'payload', NULL, NULL, 1, 1, 'pending', 0, NULL, NULL, NULL, 0, NULL,
                          '2026-01-01T00:00:00Z', NULL)
                """,
                (f"digest-{index}", f"ref-{index}", principal, project),
            )
    store = MemoryStore(path, effective_home=tmp_path)
    meta = store.ensure_meta()
    assert meta.scope_key == b"k" * 32
    assert meta.provider_root_id == "root"
    assert meta.last_provider_timestamp_ms == 42
    assert meta.last_success_at == "2026-02-03T00:00:00Z"
    with sqlite3.connect(store.path) as conn:
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT project_id FROM memory_projects WHERE principal_id = ?",
                (principal,),
            )
        } == set(legacy_projects)
    admission = store.admit_volatile_capture(
        source_message_id="new-named-project",
        session_id="session",
        principal_id=principal,
        project_ref="notes",
        provenance="user_input",
        occurred_at_ms=43,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert admission.outcome == "accepted"
    with sqlite3.connect(store.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not str(row[0]).startswith("sqlite_")
        }
        assert tables == {"memory_meta", "memory_projects"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_provider_session_ref_keeps_released_digest_and_epoch_suffix() -> None:
    scope_key = bytes(range(32))
    principal = "u-" + "a" * 32
    project = "default"
    raw_session = "session:with:colons"
    epoch = 7
    expected = hmac.new(
        scope_key,
        f"{principal}:{project}:{raw_session}".encode(),
        hashlib.sha256,
    ).hexdigest()

    assert _provider_session_ref(
        scope_key,
        principal,
        project,
        raw_session,
        epoch,
    ) == f"src--{expected}--e{epoch}"


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_unknown_released_store_shape_is_left_untouched(
    tmp_path: Path,
    version: int,
) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE memory_meta (singleton INTEGER PRIMARY KEY, scope_key BLOB)"
        )
        conn.execute("INSERT INTO memory_meta VALUES (1, ?)", (b"keep-me",))
        conn.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="Unsupported Memory store schema"):
        MemoryStore(path, effective_home=tmp_path)

    assert path.read_bytes() == before
    assert not path.with_name(f"{path.name}-wal").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute(
            "SELECT scope_key FROM memory_meta WHERE singleton = 1"
        ).fetchone()[0] == b"keep-me"


@pytest.mark.parametrize(
    ("version", "schema_path"),
    [
        (0, "tests/fixtures/memory_initial_foundation_v0.sql"),
        (0, "tests/fixtures/memory_foundation_v0.sql"),
        (1, "tests/fixtures/memory_foundation_v1.sql"),
        (3, "core/memory/schema_v2.sql"),
    ],
)
def test_released_identity_shapes_migrate_without_provider_io(
    tmp_path: Path,
    version: int,
    schema_path: str,
) -> None:
    path = _store_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(Path(schema_path).read_text(encoding="utf-8"))
        if version == 3:
            conn.execute(
                """CREATE TABLE memory_projects (
                    principal_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_written_at TEXT NOT NULL,
                    PRIMARY KEY (principal_id, project_id)
                )"""
            )
        principal = "u-" + "c" * 32
        project = "p-" + ("d" * 32)
        conn.execute(
            """INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                last_provider_timestamp_ms, missed_count, last_success_at, updated_at
            ) VALUES (1, 7, 0, ?, 'legacy-root', 42, 3, ?, ?)""",
            (b"z" * 32, "2026-02-03T00:00:00Z", "2026-02-03T00:00:00Z"),
        )
        queue_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(memory_capture_queue)")
        }
        if "generation" in queue_columns:
            conn.execute(
                """INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, provider_session_ref,
                    generation, principal_id, project_ref, provenance, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, created_at
                ) VALUES ('digest', 7, 'session', 'provider-session', 1, ?, ?,
                          'user_input', 'payload', 1, 1, 'pending', ?)""",
                (principal, project, "2026-02-01T00:00:00Z"),
            )
        else:
            names = [
                "source_message_digest", "epoch", "session_id", "principal_id",
                "project_ref", "provenance", "payload_text", "occurred_at_ms",
                "provider_timestamp_ms", "state", "created_at",
            ]
            values: list[object] = [
                "digest", 7, "session", principal, project, "user_input", "payload",
                1, 1, "pending", "2026-02-01T00:00:00Z",
            ]
            if "provider_session_ref" in queue_columns:
                names.insert(3, "provider_session_ref")
                values.insert(3, "provider-session")
            placeholders = ", ".join("?" for _ in values)
            conn.execute(
                f"INSERT INTO memory_capture_queue ({', '.join(names)}) "
                f"VALUES ({placeholders})",
                values,
            )
        if version == 3:
            conn.execute(
                "INSERT INTO memory_projects VALUES (?, ?, ?, ?)",
                (
                    principal,
                    "default",
                    "2026-02-01T00:00:00Z",
                    "2026-02-01T00:00:00Z",
                ),
            )
        conn.execute(f"PRAGMA user_version = {version}")
    store = MemoryStore(path, effective_home=tmp_path)
    meta = store.ensure_meta()
    assert (meta.epoch, meta.scope_key, meta.provider_root_id) == (7, b"z" * 32, "legacy-root")
    assert meta.last_provider_timestamp_ms == 42
    assert meta.last_success_at == "2026-02-03T00:00:00Z"
    with sqlite3.connect(path) as conn:
        assert {
            str(row[0])
            for row in conn.execute(
                "SELECT project_id FROM memory_projects WHERE principal_id = ?",
                (principal,),
            )
        } == ({project, "default"} if version == 3 else {project})
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
