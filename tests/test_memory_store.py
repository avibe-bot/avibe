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
from core.memory.store import (
    MAX_MESSAGE_ATTEMPTS,
    MAX_NONTERMINAL_QUEUE_ROWS,
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
from core.memory.types import MemorySettlementRecord, ProviderSessionRef


PROJECT = "p-22222222222222222222222222222222"


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


def _deliver(store: MemoryStore, digest: str, *, session_ref: str = "shared-session") -> str:
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
    return result.row.session_id


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
            "memory_capture_queue",
            "memory_session_flush_state",
            "memory_flush_settlements",
        }.issubset(tables)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "ix_memory_capture_due" in indexes
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info('memory_capture_queue')")}
        meta_columns = {row[1] for row in conn.execute("PRAGMA table_info('memory_meta')")}
        assert {
            "principal_id",
            "project_ref",
            "provenance",
            "payload_attachments",
            "add_request_id",
            "flush_observation",
            "flush_status",
            "flush_error_code",
            "flush_request_id",
            "flush_observed_at",
            "provider_session_ref",
            "target_generation",
            "target_watermark_ms",
        }.issubset(queue_columns)
        assert {
            "processing_fault_kind",
            "processing_fault_since",
            "processing_alert_active",
            "last_error_at",
        }.issubset(meta_columns)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, created_at
                ) VALUES ('invalid', 0, 'src', 'payload', 1, 1, 'delivered', 'now')
                """
            )


def test_provider_session_ref_round_trips_without_app_identity() -> None:
    reference = ProviderSessionRef(
        principal_id="u-11111111111111111111111111111111",
        epoch=7,
        project_ref=PROJECT,
        session_id="src--provider-session--e7",
    )

    encoded = reference.serialize()
    assert "app" not in encoded
    assert ProviderSessionRef.deserialize(encoded) == reference
    assert reference.as_tuple() == (
        "u-11111111111111111111111111111111",
        7,
        PROJECT,
        "src--provider-session--e7",
    )
    with pytest.raises(ValueError):
        ProviderSessionRef.deserialize('{"principal_id":"u-only"}')


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


def test_capture_target_is_pinned_and_settlement_is_per_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    accepted = _enqueue(store, "target", occurred_at_ms=1_000)
    assert accepted.row is not None
    assert accepted.provider_session_ref == accepted.row.provider_session_ref
    assert accepted.target_generation == 0
    assert accepted.target_watermark_ms == accepted.row.provider_timestamp_ms

    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.settle(
        row,
        Delivered(add_request_id="add-target", add_status="accumulated"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled
    session_ref = accepted.provider_session_ref
    assert session_ref is not None
    assert store.mark_flush_in_flight(row.session_id, PROJECT) == 1
    assert store.record_flush_verdict(
        row.session_id,
        PROJECT,
        FlushSucceeded(request_id="flush-target", status="extracted"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    state = store.get_session_flush_state(session_ref)
    assert state is not None
    assert state.generation == 1
    assert state.flush_state == "not_due"
    assert state.watermark == accepted.row.provider_timestamp_ms
    settlements = store.list_flush_settlements(session_ref, generation=0)
    assert {record.operation_kind for record in settlements} == {"add", "flush"}
    assert any(
        record.source == "flush"
        and record.confirmed_watermark_ms == accepted.row.provider_timestamp_ms
        for record in settlements
    )

    before_duplicate = store.get_session_flush_state(session_ref)
    assert before_duplicate is not None
    duplicate = _enqueue(store, "target", occurred_at_ms=99_000)
    after_duplicate = store.get_session_flush_state(session_ref)
    assert duplicate.outcome == "duplicate"
    assert duplicate.target_generation == accepted.target_generation
    assert duplicate.target_watermark_ms == accepted.target_watermark_ms
    assert after_duplicate == before_duplicate

    next_capture = _enqueue(store, "target-next", occurred_at_ms=2_000)
    assert next_capture.target_generation == 1
    assert next_capture.target_watermark_ms == 2_000


def test_successful_flush_defers_generation_rollover_until_remaining_rows_settle(
    tmp_path: Path,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _enqueue(store, "generation-first")
    second = _enqueue(store, "generation-second", occurred_at_ms=2_000)
    assert first.row is not None and second.row is not None
    assert first.target_generation == second.target_generation == 0

    first_row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert first_row is not None
    assert store.settle(
        first_row,
        Delivered(add_request_id="add-first"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled
    assert store.mark_flush_in_flight(first_row.session_id, PROJECT) == 1
    assert store.record_flush_verdict(
        first_row.session_id,
        PROJECT,
        FlushSucceeded(request_id="flush-first", status="extracted"),
        now="2026-01-01T00:00:02.000Z",
    ) == 1

    state = store.get_session_flush_state(first.provider_session_ref)
    assert state is not None
    assert state.generation == 0
    assert state.first_unflushed_at is not None

    second_row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:03.000Z")
    assert second_row is not None
    assert store.settle(
        second_row,
        Delivered(add_request_id="add-second"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:04.000Z"),
    ).settled
    assert store.mark_flush_in_flight(second_row.session_id, PROJECT) == 1
    assert store.record_flush_verdict(
        second_row.session_id,
        PROJECT,
        FlushSucceeded(request_id="flush-second", status="extracted"),
        now="2026-01-01T00:00:05.000Z",
    ) == 1

    state = store.get_session_flush_state(first.provider_session_ref)
    assert state is not None
    assert state.generation == 1
    assert state.first_unflushed_at is None
    assert all(
        record.generation == 0
        for record in store.list_flush_settlements(first.provider_session_ref)
        if record.operation_kind == "flush"
    )


def test_successful_no_extraction_flush_confirms_the_target_watermark(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    accepted = _enqueue(store, "no-extraction")
    assert accepted.row is not None
    assert accepted.provider_session_ref is not None
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.settle(
        row,
        Delivered(add_request_id="add-no-extraction", add_status="accumulated"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled

    assert store.mark_flush_in_flight(row.session_id, PROJECT) == 1
    assert store.record_flush_verdict(
        row.session_id,
        PROJECT,
        FlushSucceeded(request_id="flush-no-extraction", status="no_extraction"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    settlement = next(
        record
        for record in store.list_flush_settlements(accepted.provider_session_ref)
        if record.operation_kind == "flush"
    )
    assert settlement.flush_state == "settled"
    assert settlement.confirmed_watermark_ms == accepted.row.provider_timestamp_ms


def test_manual_required_fence_blocks_a_later_flush_mark(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _deliver(store, "manual-first", session_ref="manual-session")
    assert store.mark_flush_in_flight(first, PROJECT) == 1
    assert store.record_flush_verdict(
        first,
        PROJECT,
        FlushUnknown(reason="timeout"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    assert store.mark_flush_in_flight(first, PROJECT) == 0


def test_claim_due_skips_a_manual_required_session(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "manual-claim-first", session_ref="manual-claim")
    assert store.mark_flush_in_flight(session_ref, PROJECT) == 1
    assert store.record_flush_verdict(
        session_ref,
        PROJECT,
        FlushUnknown(reason="timeout"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    queued = store.enqueue_request(
        source_message_id="manual-claim-second",
        session_id="manual-claim",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=2_000,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert queued.row is not None
    assert store.claim_due(lease_owner="boot", now="2026-01-01T00:00:04.000Z") is None
    assert _row_for_source(store, "manual-claim-second").state == "pending"


def test_stale_settlement_is_retained_without_mutating_live_state(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    accepted = _enqueue(store, "stale")
    assert accepted.provider_session_ref is not None
    before = store.get_session_flush_state(accepted.provider_session_ref)
    assert before is not None

    assert store.record_settlement(
        MemorySettlementRecord(
            provider_session_ref=accepted.provider_session_ref,
            generation=before.generation + 1,
            fence_epoch=before.fence_epoch + 1,
            operation_id="future-generation",
            operation_kind="flush",
            outcome="unknown",
            observed_at="2026-01-01T00:00:01.000Z",
        )
    ) is True

    after = store.get_session_flush_state(accepted.provider_session_ref)
    assert after == before
    assert len(store.list_flush_settlements(accepted.provider_session_ref)) == 1


def _create_v1_store(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    principal = "u-11111111111111111111111111111111"
    project = PROJECT
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_meta (
                singleton INTEGER PRIMARY KEY,
                epoch INTEGER NOT NULL,
                clear_in_progress INTEGER NOT NULL,
                scope_key BLOB NOT NULL,
                provider_root_id TEXT NOT NULL,
                last_provider_timestamp_ms INTEGER NOT NULL,
                missed_count INTEGER NOT NULL,
                last_success_at TEXT,
                last_error TEXT,
                last_error_at TEXT,
                processing_fault_kind TEXT,
                processing_fault_since TEXT,
                processing_alert_active INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE memory_capture_queue (
                source_message_digest TEXT PRIMARY KEY,
                epoch INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                project_ref TEXT NOT NULL,
                provenance TEXT NOT NULL,
                payload_text TEXT,
                payload_attachments TEXT,
                occurred_at_ms INTEGER NOT NULL,
                provider_timestamp_ms INTEGER NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                lease_owner TEXT,
                lease_at TEXT,
                last_error TEXT,
                add_request_id TEXT,
                flush_observation TEXT,
                flush_status TEXT,
                flush_error_code TEXT,
                flush_request_id TEXT,
                flush_observed_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO memory_meta (
                singleton, epoch, clear_in_progress, scope_key, provider_root_id,
                last_provider_timestamp_ms, missed_count, updated_at
            ) VALUES (1, 0, 0, ?, 'legacy-root', 2000, 0, '2026-01-01T00:00:00.000Z')
            """,
            (bytes.fromhex("11" * 32),),
        )
        conn.executemany(
            """
            INSERT INTO memory_capture_queue (
                source_message_digest, epoch, session_id, principal_id, project_ref,
                provenance, payload_text, occurred_at_ms, provider_timestamp_ms,
                state, attempts, created_at, completed_at, flush_observation,
                flush_observed_at
            ) VALUES (?, 0, 'legacy-wire', ?, ?, 'user_input', ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy-unknown",
                    principal,
                    project,
                    None,
                    1_000,
                    1_000,
                    "delivered",
                    "2026-01-01T00:00:00.100Z",
                    "2026-01-01T00:00:00.200Z",
                    "unknown",
                    "2026-01-01T00:00:00.200Z",
                ),
                (
                    "legacy-in-flight",
                    principal,
                    project,
                    "recoverable payload",
                    2_000,
                    2_000,
                    "processing",
                    "2026-01-01T00:00:00.300Z",
                    None,
                    "in_flight",
                    None,
                ),
            ],
        )
        conn.execute("PRAGMA user_version = 0")


def test_v1_store_migrates_unknown_rows_conservatively_and_idempotently(tmp_path: Path) -> None:
    database = _store_path(tmp_path)
    _create_v1_store(database)

    store = MemoryStore(database)
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_capture_queue)")}
        assert {"provider_session_ref", "target_generation", "target_watermark_ms", "app"} <= queue_columns

    rows = store.list_queue_rows()
    assert [row.flush_observation for row in rows] == ["unknown", "in_flight"]
    assert all(row.provider_session_ref is not None for row in rows)
    state = store.list_session_flush_states()
    assert len(state) == 1
    assert state[0].flush_state == "manual_required"
    assert state[0].fence_epoch == 1
    settlements = store.list_flush_settlements()
    assert len(settlements) == 2
    assert {record.outcome for record in settlements} == {"manual_required"}

    MemoryStore(database)
    assert len(store.list_flush_settlements()) == 2


def test_v1_migration_projects_the_latest_flush_verdict_chronologically(tmp_path: Path) -> None:
    database = _store_path(tmp_path)
    _create_v1_store(database)
    principal = "u-11111111111111111111111111111111"

    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM memory_capture_queue")
        conn.executemany(
            """
            INSERT INTO memory_capture_queue (
                source_message_digest, epoch, session_id, principal_id, project_ref,
                provenance, payload_text, occurred_at_ms, provider_timestamp_ms,
                state, attempts, created_at, completed_at, flush_observation,
                flush_observed_at
            ) VALUES (?, 0, 'legacy-wire', ?, ?, 'user_input', NULL, ?, ?,
                      'delivered', 0, ?, ?, ?, ?)
            """,
            [
                (
                    "legacy-success",
                    principal,
                    PROJECT,
                    1_000,
                    1_000,
                    "2026-01-01T00:00:00.100Z",
                    "2026-01-01T00:00:00.200Z",
                    "succeeded",
                    "2026-01-01T00:00:00.200Z",
                ),
                (
                    "legacy-rejected",
                    principal,
                    PROJECT,
                    2_000,
                    2_000,
                    "2026-01-01T00:00:00.300Z",
                    "2026-01-01T00:00:00.400Z",
                    "rejected",
                    "2026-01-01T00:00:00.400Z",
                ),
            ],
        )

    store = MemoryStore(database)
    state = store.list_session_flush_states()
    assert len(state) == 1
    assert state[0].flush_state == "due"
    assert [record.outcome for record in store.list_flush_settlements()] == [
        "succeeded",
        "rejected",
    ]


def test_newer_memory_schema_is_rejected_before_schema_ddl(tmp_path: Path) -> None:
    database = _store_path(tmp_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE future_marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO future_marker VALUES ('untouched')")
        conn.execute("PRAGMA user_version = 3")

    with pytest.raises(OSError, match="schema is newer"):
        MemoryStore(database)

    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables == {"future_marker"}
        assert conn.execute("SELECT value FROM future_marker").fetchone()[0] == "untouched"

def test_store_assigns_one_flush_verdict_to_the_in_flight_session_group(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "one")
    assert _deliver(store, "two") == session_ref

    assert store.mark_flush_in_flight(session_ref, PROJECT) == 2
    assert [row.flush_observation for row in store.list_queue_rows()] == ["in_flight", "in_flight"]

    assert store.record_flush_verdict(
        session_ref,
        PROJECT,
        FlushSucceeded(request_id="flush-request", status="extracted"),
        now="2026-01-01T00:00:03.000Z",
    ) == 2
    rows = store.list_queue_rows()
    assert [row.flush_observation for row in rows] == ["succeeded", "succeeded"]
    assert [row.flush_status for row in rows] == ["extracted", "extracted"]
    assert [row.flush_request_id for row in rows] == ["flush-request", "flush-request"]
    assert store.ensure_meta().last_success_at == "2026-01-01T00:00:03.000Z"

    stats = store.queue_stats()
    assert stats.awaiting_receipt == 0
    assert stats.succeeded == 2
    assert stats.receipt_unknown == 0
    assert stats.distill_failed == 0
    assert stats.last_flush_observation == "succeeded"
    assert stats.last_flush_status == "extracted"


def test_store_records_rejected_and_unknown_as_terminal_observations(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    rejected_session = _deliver(store, "rejected", session_ref="rejected-session")
    assert store.mark_flush_in_flight(rejected_session, PROJECT) == 1
    assert store.record_flush_verdict(
        rejected_session,
        PROJECT,
        FlushRejected(
            request_id="reject-request",
            error_code="INTERNAL_ERROR",
            server_fault=True,
        ),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    unknown_session = _deliver(store, "unknown", session_ref="unknown-session")
    assert store.mark_flush_in_flight(unknown_session, PROJECT) == 1
    assert store.record_flush_verdict(
        unknown_session,
        PROJECT,
        FlushUnknown(reason="timeout"),
        now="2026-01-01T00:00:04.000Z",
    ) == 1

    stats = store.queue_stats()
    assert stats.succeeded == 0
    assert stats.receipt_unknown == 1
    assert stats.distill_failed == 1
    assert stats.last_flush_observation == "unknown"
    assert _row_for_source(store, "rejected").flush_error_code == "INTERNAL_ERROR"
    rejected_settlement = next(
        record
        for record in store.list_flush_settlements()
        if record.provider_session_ref is not None
        and record.provider_session_ref.session_id == rejected_session
        and record.operation_kind == "flush"
    )
    assert rejected_settlement.error_code == "INTERNAL_ERROR"


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


def test_store_activation_recovery_marks_in_flight_unknown_and_lists_unattempted_sessions(
    tmp_path: Path,
) -> None:
    store = MemoryStore(_store_path(tmp_path))
    in_flight_session = _deliver(store, "in-flight", session_ref="in-flight-session")
    not_attempted_session = _deliver(store, "not-attempted", session_ref="not-attempted-session")
    assert store.mark_flush_in_flight(in_flight_session, PROJECT) == 1

    recovery = store.recover_after_boot(
        lease_owner="boot",
        clock=lambda: _dt("2026-01-01T00:00:05.000Z"),
    )

    assert recovery.interrupted_flushes == 1
    assert _row_for_source(store, "in-flight").flush_observation == "unknown"
    # Sessions are listed only after interrupted flushes have been resolved;
    # recover_after_boot owns that ordering.
    assert recovery.not_attempted_sessions == ((not_attempted_session, PROJECT),)


def test_boot_recovery_samples_its_clock_after_reclaiming_leases(tmp_path: Path) -> None:
    """Reclamation can block on SQLite contention; the flush stamp must postdate it.

    A backdated `flush_observed_at` reorders the `ORDER BY
    COALESCE(flush_observed_at, ...)` history, so the sampling point is part of
    this method's contract rather than a caller's detail.
    """

    store = MemoryStore(_store_path(tmp_path / "recovery-clock-order"))
    in_flight_session = _deliver(store, "in-flight", session_ref="in-flight-session")
    assert store.mark_flush_in_flight(in_flight_session, PROJECT) == 1
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
    assert observed_states == ["pending"], "the clock was sampled before leases were reclaimed"
    assert _row_for_source(store, "in-flight").flush_observed_at == "2026-01-01T00:00:09.000Z"


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


def test_terminal_tombstones_compact_by_retention(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "terminal")
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None
    assert store.settle(row, Delivered(), lease_owner="boot", now=_dt("2026-01-01T00:00:01.000Z")).settled

    reference = datetime(2026, 7, 1, tzinfo=UTC)
    old = reference - TERMINAL_TOMBSTONE_RETENTION - timedelta(seconds=1)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE memory_capture_queue SET completed_at = ? WHERE source_message_digest = 'terminal'",
            (old.isoformat().replace("+00:00", "Z"),),
        )

    assert store.compact_terminal_tombstones(now=reference) == 1
    assert _row_for_source(store, "terminal") is None


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
