from __future__ import annotations

import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory.store import (
    MAX_NONTERMINAL_QUEUE_ROWS,
    MemoryStore,
    TERMINAL_TOMBSTONE_RETENTION,
)


def _store_path(scope: Path, filename: str = "memory.sqlite") -> Path:
    return paths.get_state_dir() / "memory-tests" / scope.name / filename


def _enqueue(store: MemoryStore, digest: str, *, occurred_at_ms: int = 1_000):
    return store.enqueue_capture(
        source_message_digest=digest,
        session_ref="src--digest--e0",
        payload_text="queued payload",
        occurred_at_ms=occurred_at_ms,
        max_provider_timestamp_ms=4_102_444_800_000,
    )


def test_store_creates_exact_memory_tables_and_due_index(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))

    with sqlite3.connect(store.path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list('memory_capture_queue')")
        }
        assert {"memory_meta", "memory_capture_queue"}.issubset(tables)
        assert "ix_memory_capture_due" in indexes
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, created_at
                ) VALUES ('invalid', 0, 'src', 'payload', 1, 1, 'delivered', 'now')
                """
            )


def test_duplicate_enqueue_is_atomic_and_does_not_advance_provider_clock(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))

    first = _enqueue(store, "same", occurred_at_ms=5_000)
    duplicate = _enqueue(store, "same", occurred_at_ms=99_000)
    second = _enqueue(store, "other", occurred_at_ms=5_000)

    assert first.outcome == "accepted"
    assert duplicate.outcome == "duplicate"
    assert second.outcome == "accepted"
    assert first.row is not None and second.row is not None
    assert first.row.provider_timestamp_ms == 5_000
    assert second.row.provider_timestamp_ms == 5_001
    assert store.ensure_meta().last_provider_timestamp_ms == 5_001


def test_concurrent_duplicate_enqueue_has_one_row(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _enqueue(store, "same").outcome, range(2)))

    assert sorted(outcomes) == ["accepted", "duplicate"]
    assert len(store.list_queue_rows()) == 1


def test_queue_cap_and_claim_fence(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    accepted = store.enqueue_capture(
        source_message_digest="one",
        session_ref="src--one--e0",
        payload_text="payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
        nonterminal_limit=1,
    )
    full = store.enqueue_capture(
        source_message_digest="two",
        session_ref="src--two--e0",
        payload_text="payload",
        occurred_at_ms=2,
        max_provider_timestamp_ms=100,
        nonterminal_limit=1,
    )
    assert accepted.outcome == "accepted"
    assert full.outcome == "queue_full"

    row = store.claim_due(lease_owner="boot-a", now="2026-01-01T00:00:00.000Z")
    assert row is not None and row.state == "processing"
    assert store.mark_delivered(row, lease_owner="boot-b", now="2026-01-01T00:00:01.000Z") is False
    assert store.mark_delivered(row, lease_owner="boot-a", now="2026-01-01T00:00:01.000Z") is True
    delivered = store.get_queue_row("one")
    assert delivered is not None
    assert delivered.state == "delivered"
    assert delivered.payload_text is None


def test_reclaim_processing_and_clear_deletes_every_queue_row(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "queued")
    claimed = store.claim_due(lease_owner="old-boot", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None

    assert store.reclaim_processing(lease_owner="new-boot") == 1
    reclaimed = store.get_queue_row("queued")
    assert reclaimed is not None
    assert reclaimed.state == "pending"
    assert reclaimed.attempts == 0

    before = store.ensure_meta()
    clearing = store.begin_clear()
    assert clearing.epoch == before.epoch + 1
    assert clearing.clear_in_progress is True
    completed = store.finish_clear()
    assert completed.clear_in_progress is False
    assert completed.epoch == clearing.epoch
    assert store.list_queue_rows() == ()


def test_terminal_tombstones_compact_by_retention(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "terminal")
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.mark_delivered(row, lease_owner="boot", now="2026-01-01T00:00:01.000Z")

    reference = datetime(2026, 7, 1, tzinfo=UTC)
    old = reference - TERMINAL_TOMBSTONE_RETENTION - timedelta(seconds=1)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE memory_capture_queue SET completed_at = ? WHERE source_message_digest = 'terminal'",
            (old.isoformat().replace("+00:00", "Z"),),
        )

    assert store.compact_terminal_tombstones(now=reference) == 1
    assert store.get_queue_row("terminal") is None


def test_default_store_path_uses_effective_avibe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    effective_home = tmp_path / "effective-avibe-home"
    monkeypatch.setenv("AVIBE_HOME", str(effective_home))

    store = MemoryStore()

    assert store.path == (effective_home / "state" / "memory" / "memory.sqlite").resolve()
    assert store.path.is_file()
    assert MAX_NONTERMINAL_QUEUE_ROWS == 500


def test_store_enforces_owner_only_directory_and_database_modes_under_open_umask(tmp_path: Path) -> None:
    database = _store_path(tmp_path / "memory-private")
    original_umask = os.umask(0o022)
    try:
        store = MemoryStore(database)
    finally:
        os.umask(original_umask)

    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_opening_a_higher_version_database_never_downgrades_user_version(tmp_path: Path) -> None:
    database = _store_path(tmp_path / "future-version", "future-version.sqlite")
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version = 2")

    MemoryStore(database)

    with sqlite3.connect(database) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2


def test_store_rejects_a_symlinked_state_component_before_creating_external_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effective_home = tmp_path / "effective-home"
    external = tmp_path / "external-memory-state"
    monkeypatch.setenv("AVIBE_HOME", str(effective_home))
    memory_directory = effective_home / "state" / "memory"
    memory_directory.parent.mkdir(parents=True)
    memory_directory.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        MemoryStore()

    assert not external.exists()
