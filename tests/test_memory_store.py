from __future__ import annotations

import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import paths
from core.memory.everos import FlushRejected, FlushSucceeded, FlushUnknown
from core.memory.observations import AddRejected
from core.memory.store import (
    AmbiguousAdd,
    MAX_MESSAGE_ATTEMPTS,
    MAX_NONTERMINAL_QUEUE_ROWS,
    MEMORY_STORE_SCHEMA_VERSION,
    Delivered,
    MemoryStore,
    MessageFailure,
    SettleResult,
    SystemOutage,
    TERMINAL_TOMBSTONE_RETENTION,
    derive_project_id,
    derive_principal_id,
    _keyed_digest,
)
from core.memory.types import ProviderSessionRef


PROJECT = "p-22222222222222222222222222222222"
FOUNDATION_SCHEMAS = (
    Path(__file__).with_name("fixtures") / "memory_initial_foundation_v0.sql",
    Path(__file__).with_name("fixtures") / "memory_foundation_v0.sql",
)


def _dt(value: str) -> datetime:
    """Parse the ISO instants these tests pin, for the settle transition."""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _store_path(scope: Path, filename: str = "memory.sqlite") -> Path:
    return paths.get_state_dir() / "memory-tests" / scope.name / filename


def _enqueue(store: MemoryStore, digest: str, *, occurred_at_ms: int = 1_000):
    return store.enqueue_request(
        source_message_id=digest,
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=occurred_at_ms,
        max_provider_timestamp_ms=4_102_444_800_000,
    )


def _row_for_source(store: MemoryStore, source_message_id: str):
    meta = store.ensure_meta()
    return store.get_queue_row(_keyed_digest(meta.scope_key, source_message_id))


def _enqueue_for_scope(
    store: MemoryStore,
    source_message_id: str,
    session_id: str,
    principal_id: str,
    project_ref: str,
) -> None:
    result = store.enqueue_request(
        source_message_id=source_message_id,
        session_id=session_id,
        principal_id=principal_id,
        project_ref=project_ref,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert result.row is not None


def _deliver(
    store: MemoryStore,
    digest: str,
    *,
    session_ref: str = "shared-session",
) -> ProviderSessionRef:
    result = store.enqueue_request(
        source_message_id=digest,
        session_id=session_ref,
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert result.row is not None
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.settle(
        row,
        Delivered(add_request_id=f"add-{digest}"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled
    return result.row.provider_session_ref


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
        assert {
            "memory_meta",
            "memory_attachment_bundle",
            "memory_capture_queue",
            "memory_session_flush_state",
            "memory_flush_settlements",
        }.issubset(tables)
        assert "ix_memory_capture_due" in indexes
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info('memory_capture_queue')")}
        meta_columns = {row[1] for row in conn.execute("PRAGMA table_info('memory_meta')")}
        settlement_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('memory_flush_settlements')")
        }
        assert {
            "generation",
            "lease_token",
            "principal_id",
            "project_ref",
            "provider_session_ref",
            "provenance",
            "payload_attachments",
            "attachment_bundle_id",
            "add_request_id",
            "add_status",
        }.issubset(queue_columns)
        assert not {
            "flush_observation",
            "flush_status",
            "flush_error_code",
            "flush_request_id",
            "flush_observed_at",
        }.intersection(queue_columns)
        assert {
            "processing_fault_kind",
            "processing_fault_since",
            "processing_alert_active",
            "last_error_at",
        }.issubset(meta_columns)
        assert "recovery_origin" in settlement_columns
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, created_at
                ) VALUES ('invalid', 0, 'src', 'payload', 1, 1, 'delivered', 'now')
                """
            )


@pytest.mark.parametrize("foundation_schema", FOUNDATION_SCHEMAS, ids=("initial", "parent"))
def test_store_clean_rebuilds_nonempty_foundation_v0_before_current_indexes(
    tmp_path: Path,
    foundation_schema: Path,
) -> None:
    database = _store_path(tmp_path / foundation_schema.stem)
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.executescript(foundation_schema.read_text(encoding="utf-8"))
        conn.execute(
            """
            INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                last_provider_timestamp_ms, missed_count, updated_at
            ) VALUES (1, 0, 0, X'00', 'legacy-root', 0, 0, 'now')
            """
        )
        queue_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('memory_capture_queue')")
        }
        provider_column = (
            ", provider_session_ref" if "provider_session_ref" in queue_columns else ""
        )
        provider_value = ", 'legacy-provider-session'" if provider_column else ""
        conn.execute(
            f"""
            INSERT INTO memory_capture_queue (
                source_message_digest, epoch, session_id{provider_column},
                principal_id, project_ref, provenance, payload_text,
                occurred_at_ms, provider_timestamp_ms, state, created_at
            ) VALUES (?, 0, 'legacy-session'{provider_value}, ?, ?,
                      'user_input', 'legacy payload', 1, 1, 'pending', 'now')
            """,
            (
                "legacy-digest",
                "u-11111111111111111111111111111111",
                PROJECT,
            ),
        )

    store = MemoryStore(database)

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MEMORY_STORE_SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM memory_meta").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_capture_queue").fetchone()[0] == 0
        queue_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('memory_capture_queue')")
        }
        assert {
            "provider_session_ref",
            "generation",
            "attachment_bundle_id",
            "lease_token",
        }.issubset(queue_columns)
        assert {
            row[1] for row in conn.execute("PRAGMA index_list('memory_capture_queue')")
        } >= {"ix_memory_capture_due", "ix_memory_capture_session_generation"}
        assert {
            row[1] for row in conn.execute("PRAGMA table_info('memory_session_flush_state')")
        } >= {"provider_session_ref", "open_generation", "target_generation"}


def test_store_initializes_an_empty_v0_database(tmp_path: Path) -> None:
    database = _store_path(tmp_path / "empty-v0")
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    store = MemoryStore(database)

    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == MEMORY_STORE_SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM memory_capture_queue").fetchone()[0] == 0


def test_store_rejects_unknown_nonzero_schema_without_rebuilding(tmp_path: Path) -> None:
    database = _store_path(tmp_path / "future-schema")
    future_version = MEMORY_STORE_SCHEMA_VERSION + 1
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE future_memory_state (value TEXT NOT NULL)")
        conn.execute("INSERT INTO future_memory_state VALUES ('preserve')")
        conn.execute(f"PRAGMA user_version = {future_version}")

    with pytest.raises(
        RuntimeError,
        match=f"Unsupported Memory store schema version: {future_version}",
    ):
        MemoryStore(database)

    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert conn.execute("SELECT value FROM future_memory_state").fetchone()[0] == "preserve"


def test_session_scope_recovery_survives_store_reopen_and_separates_sessions(tmp_path: Path) -> None:
    store_path = _store_path(tmp_path)
    first_scope = ("u-11111111111111111111111111111111", PROJECT)
    second_scope = (
        "u-22222222222222222222222222222222",
        "p-33333333333333333333333333333333",
    )
    store = MemoryStore(store_path)
    _enqueue_for_scope(store, "source-a", "session-a", *first_scope)
    _enqueue_for_scope(store, "source-b", "session-b", *second_scope)

    reopened = MemoryStore(store_path)

    assert reopened.resolve_current_session_scope("session-a") == first_scope
    assert reopened.resolve_current_session_scope("session-b") == second_scope
    assert reopened.resolve_current_session_scope("absent") is None

    current_epoch = reopened.ensure_meta().epoch
    reopened.reset_for_clear(target_epoch=current_epoch + 1)

    assert reopened.resolve_current_session_scope("session-a") is None


def test_session_scope_recovery_fails_closed_when_raw_session_is_ambiguous(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue_for_scope(
        store,
        "source-a",
        "shared-session",
        "u-11111111111111111111111111111111",
        PROJECT,
    )
    _enqueue_for_scope(
        store,
        "source-b",
        "shared-session",
        "u-22222222222222222222222222222222",
        "p-33333333333333333333333333333333",
    )

    assert store.resolve_current_session_scope("shared-session") is None


def test_provider_session_ref_preserves_the_canonical_identity(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))

    first = _enqueue(store, "identity")
    duplicate = _enqueue(store, "identity", occurred_at_ms=2_000)

    assert first.row is not None and duplicate.row is not None
    reference = first.row.provider_session_ref
    assert reference.as_tuple() == (
        "u-11111111111111111111111111111111",
        0,
        PROJECT,
        first.row.session_id,
    )
    assert ProviderSessionRef.deserialize(reference.serialize()) == reference
    assert duplicate.row.provider_session_ref == reference
    with sqlite3.connect(store.path) as conn:
        persisted = conn.execute(
            "SELECT provider_session_ref FROM memory_capture_queue"
        ).fetchone()[0]
    assert persisted == reference.serialize()


def test_ambiguous_add_is_terminal_and_never_claimed_again(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "ambiguous")
    claimed = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None

    result = store.settle(
        claimed,
        AmbiguousAdd(add_request_id="provider-request"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:01.000Z"),
    )

    assert result == SettleResult(settled=True, state="manual_required")
    row = _row_for_source(store, "ambiguous")
    assert row is not None
    assert (row.state, row.payload_text, row.last_error, row.add_request_id) == (
        "manual_required",
        "queued payload",
        "memory_provider_response_invalid",
        "provider-request",
    )
    assert store.has_manual_required_fence() is True
    assert store.claim_due(lease_owner="worker-2", now="2026-01-01T00:00:02.000Z") is None
    meta = store.ensure_meta()
    assert meta.last_error == "memory_processing_failed"
    assert meta.processing_fault_since == "2026-01-01T00:00:01.000Z"
    failures = store.failure_log()
    assert len(failures) == 1
    assert (failures[0].kind, failures[0].operation) == ("result_unknown", "add")
    stats = store.queue_stats()
    assert stats.receipt_unknown == 1
    assert stats.queue_plaintext_bytes == len("queued payload")
    assert (
        store.enqueue_request(
            source_message_id="bounded-after-manual",
            session_id="other-session",
            principal_id="u-11111111111111111111111111111111",
            project_ref=PROJECT,
            provenance="user_input",
            payload_text="another payload",
            occurred_at_ms=2_000,
            max_provider_timestamp_ms=4_102_444_800_000,
            nonterminal_limit=1,
        ).outcome
        == "queue_full"
    )
    follow_up = store.enqueue_request(
        source_message_id="same-session-after-manual",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="follow-up payload",
        occurred_at_ms=2_000,
        max_provider_timestamp_ms=4_102_444_800_000,
        nonterminal_limit=2,
    )
    assert follow_up.outcome == "accepted"
    assert follow_up.row is not None
    assert follow_up.row.provider_session_ref == row.provider_session_ref
    assert store.claim_due(lease_owner="worker-3", now="2026-01-01T00:00:02.000Z") is None


def test_ambiguous_add_and_processing_fault_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "atomic-ambiguous")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None

    def fail_fault_open(_conn, *, now: str) -> bool:
        del now
        raise OSError("injected processing fault write failure")

    monkeypatch.setattr(
        store,
        "_open_processing_fault_in_connection",
        fail_fault_open,
    )
    with pytest.raises(OSError, match="injected processing fault write failure"):
        store.settle(
            claimed,
            AmbiguousAdd(error="memory_provider_timeout"),
            lease_owner="worker",
            now=_dt("2026-01-01T00:00:01.000Z"),
        )

    row = _row_for_source(store, "atomic-ambiguous")
    assert row is not None and row.state == "processing"
    state = store.get_session_flush_state(claimed.provider_session_ref)
    assert state is not None and state.state == "idle"
    assert store.ensure_meta().processing_fault_since is None
    assert store.failure_log() == ()


def test_server_rejected_add_and_processing_fault_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "atomic-server-rejection")
    claimed = store.claim_due(
        lease_owner="worker",
        now="2026-01-01T00:00:00.000Z",
    )
    assert claimed is not None

    def fail_fault_open(_conn, *, now: str) -> bool:
        del now
        raise OSError("injected processing fault write failure")

    monkeypatch.setattr(
        store,
        "_open_processing_fault_in_connection",
        fail_fault_open,
    )
    with pytest.raises(OSError, match="injected processing fault write failure"):
        store.settle(
            claimed,
            AddRejected(
                request_id="server-rejection",
                error_code="INTERNAL_ERROR",
                server_fault=True,
            ),
            lease_owner="worker",
            now=_dt("2026-01-01T00:00:01.000Z"),
        )

    row = _row_for_source(store, "atomic-server-rejection")
    assert row is not None and row.state == "processing"
    assert row.attempts == 0
    assert store.ensure_meta().processing_fault_since is None
    assert store.failure_log() == ()


def test_exhausted_flush_retry_and_processing_fault_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "atomic-flush-retry")
    lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=session_ref,
        force=True,
    )
    assert lease is not None
    for second in range(3, 6):
        settled = store.retry_unsubmitted_flush(
            lease,
            now=_dt(f"2026-01-01T00:00:0{second}.000Z"),
        )
        assert (settled.settled, settled.state) == (True, "due")

    def fail_fault_open(_conn, *, now: str) -> bool:
        del now
        raise OSError("injected processing fault write failure")

    monkeypatch.setattr(
        store,
        "_open_processing_fault_in_connection",
        fail_fault_open,
    )
    with pytest.raises(OSError, match="injected processing fault write failure"):
        store.retry_unsubmitted_flush(
            lease,
            now=_dt("2026-01-01T00:00:06.000Z"),
        )

    state = store.get_session_flush_state(session_ref)
    assert state is not None
    assert (state.state, state.retry_count) == ("due", 3)
    assert store.ensure_meta().processing_fault_since is None
    assert store.failure_log() == ()


def test_principal_derivation_is_stable_opaque_and_user_scoped() -> None:
    scope_key = bytes.fromhex("11" * 32)

    first = derive_principal_id(scope_key, "slack:U123")
    assert first == derive_principal_id(scope_key, "slack:U123")
    assert first != derive_principal_id(scope_key, "slack:U456")
    assert first != derive_principal_id(bytes.fromhex("22" * 32), "slack:U123")
    assert first.startswith("u-") and len(first) == 34
    assert "U123" not in first


def test_project_derivation_is_stable_opaque_and_workdir_scoped() -> None:
    scope_key = bytes.fromhex("11" * 32)

    first = derive_project_id(scope_key, "/workspaces/one")
    assert first == derive_project_id(scope_key, "/workspaces/one")
    assert first != derive_project_id(scope_key, "/workspaces/two")
    assert first != derive_project_id(bytes.fromhex("22" * 32), "/workspaces/one")
    assert first.startswith("p-") and len(first) == 34
    assert "workspaces" not in first

    with pytest.raises(ValueError, match="workdir"):
        derive_project_id(scope_key, "relative/project")
    with pytest.raises(ValueError, match="workdir"):
        derive_project_id(scope_key, "/workspaces/../one")


def test_reused_memory_session_anchor_is_namespaced_by_project(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _enqueue(store, "first")
    second = store.enqueue_request(
        source_message_id="second",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref="p-33333333333333333333333333333333",
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )

    assert first.outcome == second.outcome == "accepted"
    assert first.row is not None and second.row is not None
    assert first.row.session_id != second.row.session_id
    assert first.row.project_ref != second.row.project_ref

def test_store_settles_one_fenced_generation_not_individual_queue_rows(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "one")
    assert _deliver(store, "two") == session_ref

    before = store.get_session_flush_state(session_ref)
    assert before is not None
    assert (before.state, before.open_generation, before.unflushed_count) == ("idle", 1, 2)

    lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=session_ref,
        force=True,
    )
    assert lease is not None
    assert lease.generation == 1
    assert store.mark_flush_submission_started(lease, now="2026-01-01T00:00:02.500Z")
    settled = store.settle_flush(
        lease,
        FlushSucceeded(request_id="flush-request", status="extracted"),
        now="2026-01-01T00:00:03.000Z",
    )
    assert (settled.settled, settled.state) == (True, "idle")

    rows = store.list_queue_rows()
    assert [row.state for row in rows] == ["delivered", "delivered"]
    after = store.get_session_flush_state(session_ref)
    assert after is not None
    assert (after.state, after.open_generation, after.unflushed_count) == ("idle", 2, 0)
    assert store.ensure_meta().last_success_at == "2026-01-01T00:00:01.000Z"

    with sqlite3.connect(store.path) as conn:
        settlement = conn.execute(
            """
            SELECT generation, operation_kind, observation, request_id
            FROM memory_flush_settlements
            WHERE operation_kind = 'flush'
            """
        ).fetchone()
        assert settlement == (1, "flush", "settled", "flush-request")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE memory_flush_settlements SET observation = 'rejected'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM memory_flush_settlements")

    clearing = store.begin_clear()
    assert clearing.clear_in_progress is True
    assert store.finish_clear().clear_in_progress is False
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_flush_settlements").fetchone()[0] == 0


def test_store_records_rejected_and_unknown_as_generation_settlements(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    rejected_session = _deliver(store, "rejected", session_ref="rejected-session")
    rejected_lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=rejected_session,
        force=True,
    )
    assert rejected_lease is not None
    assert store.mark_flush_submission_started(
        rejected_lease,
        now="2026-01-01T00:00:02.500Z",
    )
    rejected = store.settle_flush(
        rejected_lease,
        FlushRejected(
            request_id="reject-request",
            error_code="INTERNAL_ERROR",
            server_fault=True,
        ),
        now="2026-01-01T00:00:03.000Z",
    )
    assert (rejected.settled, rejected.state) == (True, "idle")

    unknown_session = _deliver(store, "unknown", session_ref="unknown-session")
    unknown_lease = store.acquire_flush(
        now="2026-01-01T00:00:03.000Z",
        provider_session_ref=unknown_session,
        force=True,
    )
    assert unknown_lease is not None
    assert store.mark_flush_submission_started(
        unknown_lease,
        now="2026-01-01T00:00:03.500Z",
    )
    unknown = store.settle_flush(
        unknown_lease,
        FlushUnknown(reason="timeout"),
        now="2026-01-01T00:00:04.000Z",
    )
    assert (unknown.settled, unknown.state) == (True, "manual_required")

    rejected_state = store.get_session_flush_state(rejected_session)
    unknown_state = store.get_session_flush_state(unknown_session)
    assert rejected_state is not None and rejected_state.state == "idle"
    assert unknown_state is not None and unknown_state.state == "manual_required"
    with sqlite3.connect(store.path) as conn:
        settlements = conn.execute(
            """
            SELECT observation, request_id, error_code
            FROM memory_flush_settlements
            WHERE operation_kind = 'flush'
            ORDER BY observed_at
            """
        ).fetchall()
    assert settlements == [
        ("rejected", "reject-request", "INTERNAL_ERROR"),
        ("manual_required", None, "memory_provider_timeout"),
    ]


def test_settle_releases_a_system_outage_without_spending_an_attempt(tmp_path: Path) -> None:
    """An outage is not this row's fault: it returns to pending, attempts intact."""

    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "outage")
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None

    result = store.settle(
        row,
        SystemOutage(error="memory_sidecar_unavailable"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    )

    assert result == SettleResult(settled=True, state="pending", attempts=None)
    released = _row_for_source(store, "outage")
    assert released is not None
    assert released.state == "pending"
    assert released.attempts == 0
    # The payload survives so the row can be delivered once the outage clears.
    assert released.payload_text == "queued payload"
    assert released.last_error == "memory_sidecar_unavailable"


def test_settle_spends_attempts_then_scrubs_a_failing_row_terminally(tmp_path: Path) -> None:
    """A row that keeps failing is retried MAX_MESSAGE_ATTEMPTS times, then dies."""

    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "poison")

    # Each retry is fenced behind a backoff, so every claim moves past the last
    # next_retry_at the store wrote: +30s after the first failure, +2min after
    # the second.
    attempt_times = ["01:00:00", "01:01:00", "01:05:00"]
    assert len(attempt_times) == MAX_MESSAGE_ATTEMPTS

    states: list[tuple[str | None, int | None]] = []
    for attempt, clock in enumerate(attempt_times, start=1):
        row = store.claim_due(lease_owner="boot", now=f"2026-01-01T{clock}.000Z")
        assert row is not None, f"row should be claimable on attempt {attempt}"
        result = store.settle(
            row,
            MessageFailure(error="memory_processing_failed"),
            lease_owner="boot",
            now=_dt(f"2026-01-01T{clock}.500Z"),
        )
        states.append((result.state, result.attempts))

    assert states == [("pending", 1), ("pending", 2), ("dead", 3)]
    dead = _row_for_source(store, "poison")
    assert dead is not None
    assert dead.state == "dead"
    # A terminal row keeps no captured text.
    assert dead.payload_text is None


def test_settle_refuses_a_row_this_owner_no_longer_holds(tmp_path: Path) -> None:
    """Every outcome is fenced by the lease, not just the delivered one."""

    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "fenced")
    row = store.claim_due(lease_owner="owner", now="2026-01-01T00:00:00.000Z")
    assert row is not None

    stolen = _dt("2026-01-01T00:00:01.000Z")
    for outcome in (
        Delivered(),
        AddRejected(request_id="stale", error_code="REJECTED", server_fault=False),
        SystemOutage(error="memory_sidecar_unavailable"),
        MessageFailure(error="memory_processing_failed"),
    ):
        result = store.settle(row, outcome, lease_owner="other-boot", now=stolen)
        assert result.settled is False, f"{outcome} must not settle another owner's claim"
        assert result.state is None

    still_claimed = _row_for_source(store, "fenced")
    assert still_claimed is not None
    assert still_claimed.state == "processing"
    assert still_claimed.attempts == 0
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_flush_settlements").fetchone()[0] == 0


def test_store_activation_recovery_fences_submitted_and_resumes_unsubmitted_flushes(
    tmp_path: Path,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    submitted_ref = _deliver(store, "in-flight", session_ref="in-flight-session")
    resumable_ref = _deliver(store, "not-attempted", session_ref="not-attempted-session")
    submitted_lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=submitted_ref,
        force=True,
    )
    resumable_lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=resumable_ref,
        force=True,
    )
    assert submitted_lease is not None and resumable_lease is not None
    assert store.mark_flush_submission_started(
        submitted_lease,
        now="2026-01-01T00:00:03.000Z",
    )

    recovery = store.recover_after_boot(
        lease_owner="boot",
        clock=lambda: _dt("2026-01-01T00:00:05.000Z"),
    )

    assert recovery.interrupted_flushes == 1
    assert recovery.due_flushes == 1
    submitted_state = store.get_session_flush_state(submitted_ref)
    resumable_state = store.get_session_flush_state(resumable_ref)
    assert submitted_state is not None and submitted_state.state == "manual_required"
    assert resumable_state is not None and resumable_state.state == "due"
    assert store.list_flush_candidates(
        now="2026-01-01T00:00:05.000Z",
    ) == (resumable_ref,)
    assert store.acquire_flush(
        now="2026-01-01T00:00:05.000Z",
        provider_session_ref=submitted_ref,
        force=True,
    ) is None
    assert store.acquire_flush(
        now="2026-01-01T00:00:05.000Z",
        provider_session_ref=resumable_ref,
    ) == resumable_lease
    failures = store.failure_log()
    assert len(failures) == 1
    assert (
        failures[0].kind,
        failures[0].operation,
        failures[0].state,
    ) == ("boot_recovery", "flush", "manual_required")


def test_boot_recovery_samples_its_clock_after_reclaiming_leases(tmp_path: Path) -> None:
    """Recovery timestamps submitted-flush evidence after reclaiming add leases."""

    store = MemoryStore(_store_path(tmp_path / "recovery-clock-order"))
    in_flight_ref = _deliver(store, "in-flight", session_ref="in-flight-session")
    lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=in_flight_ref,
        force=True,
    )
    assert lease is not None
    assert store.mark_flush_submission_started(
        lease,
        now="2026-01-01T00:00:03.000Z",
    )
    _enqueue(store, "stale-lease")
    assert store.claim_due(lease_owner="old-boot", now="2026-01-01T00:00:00.000Z") is not None
    observed_states: list[str] = []

    def clock_observing_the_queue() -> datetime:
        observed_states.append(_row_for_source(store, "stale-lease").state)
        return _dt("2026-01-01T00:00:09.000Z")

    recovery = store.recover_after_boot(
        lease_owner="new-boot",
        clock=clock_observing_the_queue,
    )

    assert recovery.reclaimed == 1
    assert observed_states == ["manual_required"], "the clock was sampled before leases were reclaimed"
    state = store.get_session_flush_state(in_flight_ref)
    assert state is not None
    assert (state.state, state.updated_at) == (
        "manual_required",
        "2026-01-01T00:00:09.000Z",
    )
    with sqlite3.connect(store.path) as conn:
        observed_at = conn.execute(
            """
            SELECT observed_at FROM memory_flush_settlements
            WHERE operation_kind = 'flush'
            """
        ).fetchone()[0]
    assert observed_at == "2026-01-01T00:00:09.000Z"


def test_store_persists_refreshes_and_closes_processing_fault(tmp_path: Path) -> None:
    database = _store_path(tmp_path)
    store = MemoryStore(database)

    assert store.open_processing_fault(now="2026-01-01T00:00:00.000Z") is True
    assert store.classify_processing_fault("credential") is True
    assert store.mark_processing_alert_active() is True
    reopened = MemoryStore(database).ensure_meta()
    assert reopened.processing_fault_since == "2026-01-01T00:00:00.000Z"
    assert reopened.processing_fault_kind == "credential"
    assert reopened.processing_alert_active is True
    assert reopened.last_error == "memory_processing_failed"

    assert store.open_processing_fault(now="2026-01-01T00:05:00.000Z") is False
    assert store.classify_processing_fault("engine") is False
    refreshed = store.ensure_meta()
    assert refreshed.processing_fault_since == "2026-01-01T00:05:00.000Z"
    assert refreshed.processing_fault_kind == "engine"

    assert store.close_processing_fault(now="2026-01-01T00:05:01.000Z") is True
    closed = store.ensure_meta()
    assert closed.processing_fault_since is None
    assert closed.processing_fault_kind is None
    assert closed.processing_alert_active is False
    assert closed.last_error is None


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
    accepted = store.enqueue_request(
        source_message_id="one",
        session_id="one",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
        nonterminal_limit=1,
    )
    full = store.enqueue_request(
        source_message_id="two",
        session_id="two",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="payload",
        occurred_at_ms=2,
        max_provider_timestamp_ms=100,
        nonterminal_limit=1,
    )
    assert accepted.outcome == "accepted"
    assert full.outcome == "queue_full"

    row = store.claim_due(lease_owner="boot-a", now="2026-01-01T00:00:00.000Z")
    assert row is not None and row.state == "processing"
    assert store.settle(row, Delivered(), lease_owner="boot-b", now=_dt("2026-01-01T00:00:01.000Z")).settled is False
    assert store.settle(row, Delivered(), lease_owner="boot-a", now=_dt("2026-01-01T00:00:01.000Z")).settled is True
    delivered = _row_for_source(store, "one")
    assert delivered is not None
    assert delivered.state == "delivered"
    assert delivered.payload_text is None


def test_reclaim_processing_and_clear_deletes_every_queue_row(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "queued")
    claimed = store.claim_due(lease_owner="old-boot", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None

    recovery = store.recover_after_boot(
        lease_owner="new-boot",
        clock=lambda: _dt("2026-01-01T00:00:02.000Z"),
    )
    assert recovery.reclaimed == 1
    reclaimed = _row_for_source(store, "queued")
    assert reclaimed is not None
    assert reclaimed.state == "manual_required"
    assert reclaimed.attempts == 0
    assert reclaimed.payload_text == "queued payload"
    assert reclaimed.last_error == "memory_provider_response_invalid"
    assert store.has_manual_required_fence() is True
    failures = store.failure_log()
    assert len(failures) == 1
    assert (
        failures[0].kind,
        failures[0].operation,
        failures[0].state,
    ) == ("boot_recovery", "add", "manual_required")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT recovery_origin FROM memory_flush_settlements"
        ).fetchall() == [("boot",)]

    before = store.ensure_meta()
    clearing = store.begin_clear()
    assert clearing.epoch == before.epoch + 1
    assert clearing.clear_in_progress is True
    completed = store.finish_clear()
    assert completed.clear_in_progress is False
    assert completed.epoch == clearing.epoch
    assert store.list_queue_rows() == ()


def test_clear_reset_replays_at_the_exact_target_epoch(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "queued")
    lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=session_ref,
        force=True,
    )
    assert lease is not None
    assert store.mark_flush_submission_started(lease, now="2026-01-01T00:00:02.500Z")
    assert store.settle_flush(
        lease,
        FlushSucceeded(request_id="flush-before-clear", status="extracted"),
        now="2026-01-01T00:00:03.000Z",
    ).settled
    pre_epoch = store.ensure_meta().epoch

    first = store.reset_for_clear(target_epoch=pre_epoch + 1)
    replay = store.reset_for_clear(target_epoch=pre_epoch + 1)

    assert first.epoch == pre_epoch + 1
    assert replay.epoch == first.epoch
    assert store.list_queue_rows() == ()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_flush_settlements").fetchone()[0] == 0
    with pytest.raises(ValueError):
        store.reset_for_clear(target_epoch=pre_epoch + 3)


@pytest.mark.parametrize("provenance", ["user_input", "agent"])
def test_provenance_survives_payload_tombstoning(tmp_path: Path, provenance: str) -> None:
    store = MemoryStore(_store_path(tmp_path))
    result = store.enqueue_request(
        source_message_id=f"source-{provenance}",
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance=provenance,
        payload_text="private payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
    )
    assert result.row is not None
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.settle(row, Delivered(), lease_owner="boot", now=_dt("2026-01-01T00:00:01.000Z")).settled

    tombstone = store.get_queue_row(result.row.source_message_digest)
    assert tombstone is not None
    assert tombstone.payload_text is None
    assert tombstone.provenance == provenance


def test_terminal_tombstones_compact_after_90_days_but_retain_settlement_evidence(
    tmp_path: Path,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "terminal")
    lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=session_ref,
        force=True,
    )
    assert lease is not None
    assert store.mark_flush_submission_started(lease, now="2026-01-01T00:00:02.500Z")
    assert store.settle_flush(
        lease,
        FlushSucceeded(request_id="flush-terminal", status="extracted"),
        now="2026-01-01T00:00:03.000Z",
    ).settled

    reference = (
        datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)
        + TERMINAL_TOMBSTONE_RETENTION
        + timedelta(seconds=1)
    )

    assert store.compact_terminal_tombstones(now=reference) == 1
    assert _row_for_source(store, "terminal") is None
    with sqlite3.connect(store.path) as conn:
        settlement = conn.execute(
            """
            SELECT operation_kind, observation, request_id, observed_at
            FROM memory_flush_settlements
            """
        ).fetchone()
    assert settlement == (
        "flush",
        "settled",
        "flush-terminal",
        "2026-01-01T00:00:03.000Z",
    )


def test_terminal_tombstone_compaction_prunes_idle_session_state_after_high_churn(
    tmp_path: Path,
) -> None:
    store_path = _store_path(tmp_path)
    store = MemoryStore(store_path)
    session_refs: list[ProviderSessionRef] = []

    for index in range(32):
        result = store.enqueue_request(
            source_message_id=f"churn-source-{index}",
            session_id=f"churn-session-{index}",
            principal_id="u-11111111111111111111111111111111",
            project_ref=PROJECT,
            provenance="user_input",
            payload_text="queued payload",
            occurred_at_ms=1_000 + index,
            max_provider_timestamp_ms=4_102_444_800_000,
        )
        assert result.row is not None
        claimed = store.claim_due(
            lease_owner="boot",
            now="2026-01-01T00:00:00.000Z",
        )
        assert claimed is not None
        assert store.settle(
            claimed,
            Delivered(
                add_request_id=f"churn-add-{index}",
                add_status="extracted",
            ),
            lease_owner="boot",
            now=_dt("2026-01-01T00:00:01.000Z"),
        ).settled
        session_refs.append(result.row.provider_session_ref)

    assert store.resolve_current_session_scope("churn-session-0") == (
        "u-11111111111111111111111111111111",
        PROJECT,
    )
    reference = (
        datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        + TERMINAL_TOMBSTONE_RETENTION
        + timedelta(seconds=1)
    )

    assert store.compact_terminal_tombstones(now=reference) == len(session_refs)
    assert all(store.get_session_flush_state(ref) is None for ref in session_refs)

    reopened = MemoryStore(store_path)
    recovery = reopened.recover_after_boot(lease_owner="next-boot", clock=lambda: reference)
    assert (recovery.reclaimed, recovery.interrupted_flushes, recovery.due_flushes) == (0, 0, 0)
    assert reopened.resolve_current_session_scope("churn-session-0") is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_flush_settlements").fetchone()[0] == len(
            session_refs
        )


def test_terminal_tombstone_count_compaction_prunes_only_the_evicted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_refs = [
        _deliver(store, f"bounded-{index}", session_ref=f"bounded-session-{index}")
        for index in range(2)
    ]
    for index, session_ref in enumerate(session_refs):
        lease = store.acquire_flush(
            now="2026-01-01T00:00:02.000Z",
            provider_session_ref=session_ref,
            force=True,
        )
        assert lease is not None
        assert store.mark_flush_submission_started(
            lease,
            now="2026-01-01T00:00:02.500Z",
        )
        assert store.settle_flush(
            lease,
            FlushSucceeded(request_id=f"bounded-flush-{index}", status="extracted"),
            now="2026-01-01T00:00:03.000Z",
        ).settled

    monkeypatch.setattr("core.memory.store.TERMINAL_TOMBSTONE_LIMIT", 1)
    assert store.compact_terminal_tombstones(
        now=datetime(2026, 1, 2, tzinfo=UTC)
    ) == 1
    retained_states = [store.get_session_flush_state(ref) for ref in session_refs]
    assert sum(state is None for state in retained_states) == 1
    assert len(store.list_queue_rows()) == 1


def test_session_state_pruning_preserves_active_and_retained_evidence(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))

    due_ref = _deliver(store, "retained-due", session_ref="retained-due")
    due_lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=due_ref,
        force=True,
    )
    assert due_lease is not None

    in_flight_ref = _deliver(store, "retained-in-flight", session_ref="retained-in-flight")
    in_flight_lease = store.acquire_flush(
        now="2026-01-01T00:00:02.000Z",
        provider_session_ref=in_flight_ref,
        force=True,
    )
    assert in_flight_lease is not None
    assert store.mark_flush_submission_started(
        in_flight_lease,
        now="2026-01-01T00:00:02.500Z",
    )

    manual = store.enqueue_request(
        source_message_id="retained-manual",
        session_id="retained-manual",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=2_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert manual.row is not None
    manual_claim = store.claim_due(
        lease_owner="manual-boot",
        now="2026-01-01T00:00:03.000Z",
    )
    assert manual_claim is not None
    assert store.settle(
        manual_claim,
        AmbiguousAdd(),
        lease_owner="manual-boot",
        now=_dt("2026-01-01T00:00:04.000Z"),
    ).settled

    current = store.enqueue_request(
        source_message_id="retained-current",
        session_id="retained-current",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=3_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert current.row is not None
    current_claim = store.claim_due(
        lease_owner="current-boot",
        now="2026-03-31T23:59:58.000Z",
    )
    assert current_claim is not None
    assert store.settle(
        current_claim,
        Delivered(add_status="extracted"),
        lease_owner="current-boot",
        now=_dt("2026-03-31T23:59:59.000Z"),
    ).settled

    dead = store.enqueue_request(
        source_message_id="retained-dead",
        session_id="retained-dead",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=4_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert dead.row is not None
    dead_claim = store.claim_due(
        lease_owner="dead-boot",
        now="2026-03-31T23:59:58.000Z",
    )
    assert dead_claim is not None
    assert store.settle(
        dead_claim,
        MessageFailure(error="memory_processing_failed", retryable=False),
        lease_owner="dead-boot",
        now=_dt("2026-03-31T23:59:59.000Z"),
    ).settled

    pending = store.enqueue_request(
        source_message_id="retained-pending",
        session_id="retained-pending",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=5_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert pending.row is not None

    reference = datetime(2026, 4, 2, tzinfo=UTC)
    store.compact_terminal_tombstones(now=reference)

    expected_states = {
        pending.row.provider_session_ref: "idle",
        due_ref: "due",
        in_flight_ref: "in_flight",
        manual.row.provider_session_ref: "manual_required",
        current.row.provider_session_ref: "idle",
        dead.row.provider_session_ref: "idle",
    }
    for session_ref, expected_state in expected_states.items():
        state = store.get_session_flush_state(session_ref)
        assert state is not None and state.state == expected_state
    assert _row_for_source(store, "retained-pending") is not None
    assert _row_for_source(store, "retained-due") is None
    assert _row_for_source(store, "retained-in-flight") is None
    assert _row_for_source(store, "retained-manual") is not None
    assert _row_for_source(store, "retained-current") is not None
    assert _row_for_source(store, "retained-dead") is not None


def test_rejected_add_evidence_survives_terminal_tombstone_compaction(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "rejected-add")
    row = store.claim_due(lease_owner="boot", now="2099-01-01T00:00:00.000Z")
    assert row is not None
    observed_at = _dt("2099-01-01T00:00:01.000Z")

    result = store.settle(
        row,
        AddRejected(
            request_id="rejected-request",
            error_code="INTERNAL_ERROR",
            server_fault=True,
        ),
        lease_owner="boot",
        now=observed_at,
    )

    assert result == SettleResult(settled=True, state="dead", attempts=1)
    with sqlite3.connect(store.path) as conn:
        settlement = conn.execute(
            """
            SELECT operation_kind, operation_token, observation, request_id,
                   observed_at, error_code
            FROM memory_flush_settlements
            """
        ).fetchone()
    assert settlement == (
        "add",
        f"add:{row.source_message_digest}:{row.lease_token}",
        "rejected",
        "rejected-request",
        "2099-01-01T00:00:01.000Z",
        "INTERNAL_ERROR",
    )
    failures = store.failure_log()
    assert len(failures) == 1
    assert (
        failures[0].kind,
        failures[0].state,
        failures[0].operation,
        failures[0].error_code,
        failures[0].request_id,
    ) == (
        "delivery_abandoned",
        "rejected",
        "add",
        "INTERNAL_ERROR",
        "rejected-request",
    )

    reference = observed_at + TERMINAL_TOMBSTONE_RETENTION + timedelta(seconds=1)
    assert store.compact_terminal_tombstones(now=reference) == 1
    assert _row_for_source(store, "rejected-add") is None
    assert store.failure_log() == failures


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


@pytest.mark.parametrize("loses_race_at", ["chmod", "mode_verification"])
def test_store_tolerates_a_sidecar_deleted_while_modes_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loses_race_at: str,
) -> None:
    """A peer connection checkpointing away the shm file is not a store failure."""

    store = MemoryStore(_store_path(tmp_path / f"sidecar-race-{loses_race_at}"))
    sidecar = store.path.with_name(f"{store.path.name}-shm")
    sidecar.touch()
    real_chmod = os.chmod
    races: list[str] = []

    def racing_chmod(path, mode, *args, **kwargs):
        if Path(path) != sidecar:
            return real_chmod(path, mode, *args, **kwargs)
        races.append(loses_race_at)
        if loses_race_at == "chmod":
            sidecar.unlink()
            return real_chmod(path, mode, *args, **kwargs)
        result = real_chmod(path, mode, *args, **kwargs)
        sidecar.unlink()
        return result

    monkeypatch.setattr(os, "chmod", racing_chmod)

    assert store.ensure_meta() is not None
    assert races, "the sidecar race never fired, so no benign ENOENT was exercised"


def test_store_keeps_sidecar_checks_strict_for_files_that_do_exist(tmp_path: Path) -> None:
    """Tolerating a vanished sidecar must not weaken the checks on a present one."""

    store = MemoryStore(_store_path(tmp_path / "sidecar-strict"))
    sidecar = store.path.with_name(f"{store.path.name}-wal")
    sidecar.touch()
    os.chmod(sidecar, 0o644)

    store._enforce_private_database_modes()

    assert stat.S_IMODE(sidecar.lstat().st_mode) == 0o600

    sidecar.unlink()
    sidecar.symlink_to(tmp_path / "external-wal")

    with pytest.raises(OSError, match="must be a regular file"):
        store._enforce_private_database_modes()


def test_store_does_not_treat_a_vanished_main_database_as_a_benign_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only SQLite's own sidecars may disappear mid-check; the database may not."""

    store = MemoryStore(_store_path(tmp_path / "database-vanishes"))
    real_chmod = os.chmod

    def vanishing_chmod(path, mode, *args, **kwargs):
        result = real_chmod(path, mode, *args, **kwargs)
        if Path(path) == store.path:
            store.path.unlink()
        return result

    monkeypatch.setattr(os, "chmod", vanishing_chmod)

    with pytest.raises(FileNotFoundError):
        store._enforce_private_database_modes()
