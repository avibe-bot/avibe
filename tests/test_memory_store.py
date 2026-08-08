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
    MAX_FLUSH_RETRY_ATTEMPTS,
    MAX_MESSAGE_ATTEMPTS,
    MAX_NONTERMINAL_QUEUE_ROWS,
    AmbiguousAdd,
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


def _flush_claim(store: MemoryStore, provider_session_ref: ProviderSessionRef):
    token = store.mark_flush_in_flight(provider_session_ref)
    assert token is not None
    return token


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
        assert "ix_memory_capture_due" in indexes
        assert "ix_memory_capture_session_flush" in indexes
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
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
            "flush_generation",
        }.issubset(queue_columns)
        assert {
            "processing_fault_kind",
            "processing_fault_since",
            "processing_alert_active",
            "last_error_at",
        }.issubset(meta_columns)
        state_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('memory_session_flush_state')")
        }
        assert {
            "generation",
            "watermark",
            "fence_epoch",
            "fence_operation_id",
            "flush_state",
            "flush_retry_count",
        }.issubset(state_columns)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO memory_capture_queue (
                    source_message_digest, epoch, session_id, payload_text,
                    occurred_at_ms, provider_timestamp_ms, state, created_at
                ) VALUES ('invalid', 0, 'src', 'payload', 1, 1, 'delivered', 'now')
                """
            )


def test_provider_session_ref_serializes_the_exact_canonical_identity() -> None:
    reference = ProviderSessionRef(
        principal_id="u-11111111111111111111111111111111",
        epoch=7,
        project_ref=PROJECT,
        session_id="src--provider-session--e7",
    )

    assert reference.as_tuple() == (
        "u-11111111111111111111111111111111",
        7,
        PROJECT,
        "src--provider-session--e7",
    )
    assert ProviderSessionRef.deserialize(reference.serialize()) == reference
    with pytest.raises(ValueError, match="invalid provider session reference"):
        ProviderSessionRef.deserialize('{"principal_id":"u-only"}')
    with pytest.raises(ValueError, match="invalid provider session reference"):
        ProviderSessionRef.deserialize(
            '{"epoch":7,"principal_id":"u-1","project_ref":"p-1",'
            '"session_id":"s","extra":"no"}'
        )


def test_enqueue_creates_one_idempotent_session_state_for_the_canonical_ref(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _enqueue(store, "first")
    second = _enqueue(store, "second", occurred_at_ms=1_001)

    assert first.row is not None and second.row is not None
    assert first.row.provider_session_ref == second.row.provider_session_ref
    state = store.get_session_flush_state(first.row.provider_session_ref)
    assert state is not None
    assert state.provider_session_ref.as_tuple() == (
        first.row.principal_id,
        first.row.epoch,
        first.row.project_ref,
        first.row.session_id,
    )
    assert (state.generation, state.watermark, state.fence_epoch) == (0, 0, 0)
    assert len(store.list_session_flush_states()) == 1

    assert store.ensure_session_flush_state(first.row.provider_session_ref) == state


def test_due_state_probe_avoids_materializing_session_history(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "due", session_ref="due-session")

    assert store.has_due_flush_state() is False
    token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        token,
        FlushRejected("retry", "CONFLICT", server_fault=False),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    assert store.has_due_flush_state() is True


def test_claim_due_serializes_same_session_adds(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    _enqueue(store, "first")
    _enqueue(store, "second", occurred_at_ms=1_001)

    first = store.claim_due(lease_owner="first-worker", now="2026-01-01T00:00:00.000Z")
    assert first is not None
    assert store.claim_due(lease_owner="second-worker", now="2026-01-01T00:00:01.000Z") is None

    assert store.settle(
        first,
        Delivered(add_request_id="first-add"),
        lease_owner="first-worker",
        now=_dt("2026-01-01T00:00:02.000Z"),
    ).settled
    second = store.claim_due(lease_owner="second-worker", now="2026-01-01T00:00:03.000Z")
    assert second is not None


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

def test_store_assigns_one_flush_verdict_to_the_in_flight_session_group(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "one")
    assert _deliver(store, "two") == session_ref

    token = _flush_claim(store, session_ref)
    assert [row.flush_observation for row in store.list_queue_rows()] == ["in_flight", "in_flight"]

    assert store.record_flush_verdict(
        token,
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
    delivered = _row_for_source(store, "one")
    state = store.get_session_flush_state(delivered.provider_session_ref)
    assert state is not None
    assert (state.generation, state.watermark, state.fence_epoch) == (1, 1_001, 1)
    assert (state.flush_state, state.fence_owner, state.first_unflushed_at) == (
        "not_due",
        None,
        None,
    )
    settlements = store.list_flush_settlements(delivered.provider_session_ref)
    flush_settlement = next(item for item in settlements if item.operation_kind == "flush")
    assert (
        flush_settlement.generation,
        flush_settlement.confirmed_watermark_ms,
        flush_settlement.flush_state,
    ) == (0, 1_001, "settled")
    assert store.record_settlement(flush_settlement) is False


def test_store_records_rejected_and_unknown_as_terminal_observations(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    rejected_session = _deliver(store, "rejected", session_ref="rejected-session")
    rejected_token = _flush_claim(store, rejected_session)
    assert store.record_flush_verdict(
        rejected_token,
        FlushRejected(
            request_id="reject-request",
            error_code="INTERNAL_ERROR",
            server_fault=True,
        ),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    unknown_session = _deliver(store, "unknown", session_ref="unknown-session")
    unknown_token = _flush_claim(store, unknown_session)
    assert store.record_flush_verdict(
        unknown_token,
        FlushUnknown(reason="timeout"),
        now="2026-01-01T00:00:04.000Z",
    ) == 1

    stats = store.queue_stats()
    assert stats.succeeded == 0
    assert stats.receipt_unknown == 1
    assert stats.distill_failed == 1
    assert stats.last_flush_observation == "unknown"
    assert _row_for_source(store, "rejected").flush_error_code == "INTERNAL_ERROR"
    unknown_row = _row_for_source(store, "unknown")
    unknown_state = store.get_session_flush_state(unknown_row.provider_session_ref)
    assert unknown_state is not None
    assert unknown_state.flush_state == "manual_required"
    assert store.ensure_meta().last_error == "memory_processing_failed"
    unknown_settlement = store.list_flush_settlements(unknown_row.provider_session_ref)[0]
    assert (unknown_settlement.watermark_after, unknown_settlement.confirmed_watermark_ms) == (
        unknown_row.provider_timestamp_ms,
        None,
    )


def test_extracted_add_records_generation_settlement_and_advances_watermark(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    result = _enqueue(store, "natural-boundary", occurred_at_ms=2_000)
    assert result.row is not None
    row = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert row is not None

    assert store.settle(
        row,
        Delivered(add_request_id="add-natural", add_status="extracted"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled

    delivered = _row_for_source(store, "natural-boundary")
    assert (delivered.flush_observation, delivered.flush_status) == ("succeeded", "extracted")
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None
    assert (state.generation, state.watermark, state.flush_state) == (1, 2_000, "not_due")
    settlement = store.list_flush_settlements(row.provider_session_ref)
    assert len(settlement) == 1
    assert (
        settlement[0].operation_kind,
        settlement[0].generation,
        settlement[0].outcome,
        settlement[0].confirmed_watermark_ms,
        settlement[0].flush_state,
    ) == ("add", 0, "succeeded", 2_000, "settled")


def test_extracted_add_moves_pending_rows_to_the_next_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _enqueue(store, "first-natural", occurred_at_ms=1_000)
    second = _enqueue(store, "second-natural", occurred_at_ms=1_001)
    assert first.row is not None
    assert second.row is not None

    claimed = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None
    assert store.settle(
        claimed,
        Delivered(add_request_id="first-add", add_status="extracted"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled

    pending = _row_for_source(store, "second-natural")
    assert pending.flush_generation == 1
    next_claim = store.claim_due(lease_owner="worker-2", now="2026-01-01T00:00:02.000Z")
    assert next_claim is not None
    assert next_claim.flush_generation == 1
    assert store.settle(
        next_claim,
        Delivered(add_request_id="second-add", add_status="extracted"),
        lease_owner="worker-2",
        now=_dt("2026-01-01T00:00:03.000Z"),
    ).settled

    state = store.get_session_flush_state(claimed.provider_session_ref)
    assert state is not None
    assert state.generation == 2
    assert [item.generation for item in store.list_flush_settlements(claimed.provider_session_ref)] == [
        0,
        1,
    ]


def test_extracted_add_settles_its_pinned_next_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "first", session_ref="generation-natural")
    token = _flush_claim(store, provider_session_ref)
    assert store.enqueue_request(
        source_message_id="next-generation",
        session_id="generation-natural",
        principal_id=provider_session_ref.principal_id,
        project_ref=provider_session_ref.project_ref,
        provenance="user_input",
        payload_text="next generation payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    ).row.flush_generation == token.generation + 1
    assert store.record_flush_verdict(
        token,
        FlushRejected("rejected", "INVALID_INPUT", server_fault=False, retryable=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1

    row = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:03.000Z")
    assert row is not None
    assert row.flush_generation == token.generation + 1
    assert store.settle(
        row,
        Delivered(add_request_id="add-next", add_status="extracted"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:04.000Z"),
    ).settled

    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert (state.generation, state.flush_state) == (2, "not_due")
    add_settlement = next(
        item
        for item in store.list_flush_settlements(provider_session_ref)
        if item.operation_kind == "add"
    )
    assert (add_settlement.generation, add_settlement.outcome) == (1, "succeeded")


def test_claim_and_admission_respect_session_flush_fences(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "delivered", session_ref="fenced-session")
    pending = store.enqueue_request(
        source_message_id="pending",
        session_id="fenced-session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert pending.outcome == "accepted"
    _flush_claim(store, session_ref)

    assert store.claim_due(lease_owner="blocked", now="2026-01-01T00:00:02.000Z") is None
    assert store.list_queue_rows()[1].state == "pending"

    manual_store = MemoryStore(_store_path(tmp_path / "manual-fence"))
    first = _enqueue(manual_store, "manual-first")
    assert first.row is not None
    claimed = manual_store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None
    assert manual_store.settle(
        claimed,
        AmbiguousAdd(add_request_id="ambiguous"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled
    blocked = _enqueue(manual_store, "manual-second")
    assert blocked.outcome == "manual_required"
    assert manual_store.claim_due(lease_owner="blocked", now="2026-01-01T00:00:02.000Z") is None


def test_stale_flush_token_cannot_clear_newer_manual_fence(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "first", session_ref="racing-session")
    token = _flush_claim(store, provider_session_ref)

    newer_manual = MemorySettlementRecord(
        provider_session_ref=provider_session_ref,
        generation=token.generation,
        fence_epoch=token.fence_epoch + 1,
        operation_id="manual-newer",
        operation_kind="add",
        outcome="manual_required",
        observed_at="2026-01-01T00:00:03.000Z",
        last_known_state="pending",
        last_observed_outcome="manual_required",
        flush_state="manual_required",
        source="add",
    )
    with store._transaction() as conn:
        store._mark_manual_required_in_connection(
            conn,
            newer_manual,
            now="2026-01-01T00:00:03.000Z",
        )
    assert store.record_flush_verdict(
        token,
        FlushSucceeded("stale-flush", "extracted"),
        now="2026-01-01T00:00:04.000Z",
    ) == 0

    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert state.flush_state == "manual_required"
    assert state.fence_operation_id == "manual-newer"
    assert store.list_queue_rows()[0].flush_observation == "in_flight"
    assert [item.outcome for item in store.list_flush_settlements(provider_session_ref)] == [
        "manual_required"
    ]


def test_ambiguous_add_settlement_uses_its_pinned_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "first", session_ref="ambiguous-next-generation")
    token = _flush_claim(store, provider_session_ref)
    accepted = store.enqueue_request(
        source_message_id="ambiguous-next",
        session_id="ambiguous-next-generation",
        principal_id=provider_session_ref.principal_id,
        project_ref=provider_session_ref.project_ref,
        provenance="user_input",
        payload_text="ambiguous next-generation payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None
    assert accepted.row.flush_generation == token.generation + 1
    assert store.record_flush_verdict(
        token,
        FlushRejected("rejected", "INVALID_INPUT", server_fault=False, retryable=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1

    row = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:03.000Z")
    assert row is not None
    assert store.settle(
        row,
        AmbiguousAdd(add_request_id="ambiguous-add"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:04.000Z"),
    ).settled

    add_settlement = next(
        item
        for item in store.list_flush_settlements(provider_session_ref)
        if item.operation_kind == "add"
    )
    assert (add_settlement.generation, add_settlement.outcome) == (
        token.generation + 1,
        "manual_required",
    )


def test_flush_claim_waits_for_a_processing_add(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "delivered", session_ref="processing-session")
    pending = store.enqueue_request(
        source_message_id="processing",
        session_id="processing-session",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert pending.outcome == "accepted"
    processing = store.claim_due(lease_owner="add-worker", now="2026-01-01T00:00:02.000Z")
    assert processing is not None

    assert store.mark_flush_in_flight(provider_session_ref) is None
    assert store.list_queue_rows()[0].flush_observation == "not_attempted"
    assert store.get_session_flush_state(provider_session_ref).flush_state == "not_due"


def test_enqueue_admits_next_generation_while_claims_wait_for_flush(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "in-flight-admission")
    token = _flush_claim(store, provider_session_ref)

    accepted = store.enqueue_request(
        source_message_id="blocked-during-flush",
        session_id="shared-session",
        principal_id=provider_session_ref.principal_id,
        project_ref=provider_session_ref.project_ref,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )

    assert accepted.outcome == "accepted"
    assert accepted.row is not None
    assert accepted.row.flush_generation == token.generation + 1
    assert len(store.list_queue_rows()) == 2
    assert store.claim_due(lease_owner="blocked", now="2026-01-01T00:00:03.000Z") is None

    assert store.record_flush_verdict(
        token,
        FlushSucceeded("flush", "extracted"),
        now="2026-01-01T00:00:04.000Z",
    ) == 1
    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert state.generation == token.generation + 1

    claimed = store.claim_due(lease_owner="after-flush", now="2026-01-01T00:00:05.000Z")
    assert claimed is not None
    assert claimed.flush_generation == state.generation


def test_flush_acquisition_moves_pending_rows_to_the_next_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    first = _enqueue(store, "first-pending", occurred_at_ms=1_000)
    second = _enqueue(store, "second-pending", occurred_at_ms=1_001)
    assert first.row is not None
    assert second.row is not None

    claimed = store.claim_due(lease_owner="worker", now="2026-01-01T00:00:00.000Z")
    assert claimed is not None
    assert store.settle(
        claimed,
        Delivered(add_request_id="first-add"),
        lease_owner="worker",
        now=_dt("2026-01-01T00:00:01.000Z"),
    ).settled

    token = _flush_claim(store, claimed.provider_session_ref)
    pending = _row_for_source(store, "second-pending")
    assert pending.flush_generation == token.generation + 1

    assert store.record_flush_verdict(
        token,
        FlushSucceeded("flush", "extracted"),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    next_claim = store.claim_due(lease_owner="worker-2", now="2026-01-01T00:00:03.000Z")
    assert next_claim is not None
    assert next_claim.flush_generation == token.generation + 1


def test_due_rejected_generation_can_acquire_a_retry_token(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "retryable")
    first_token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        first_token,
        FlushRejected("rejected", "TEMPORARY", server_fault=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    rejected = store.list_queue_rows()[0]
    assert rejected.flush_observation == "rejected"
    due = store.get_session_flush_state(provider_session_ref)
    assert due is not None
    assert due.flush_state == "due"

    retry_token = store.mark_flush_in_flight(provider_session_ref)
    assert retry_token is not None
    assert retry_token.generation == first_token.generation
    assert retry_token.fence_epoch > first_token.fence_epoch
    assert store.list_queue_rows()[0].flush_observation == "in_flight"
    assert store.record_flush_verdict(
        retry_token,
        FlushSucceeded("retried", "extracted"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1
    settled = store.get_session_flush_state(provider_session_ref)
    assert settled is not None
    assert (settled.generation, settled.flush_state) == (1, "not_due")


def test_claim_due_blocks_new_adds_until_due_generation_is_retried(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "first", session_ref="due-session")
    first_token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        first_token,
        FlushRejected("rejected", "TEMPORARY", server_fault=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1

    second = store.enqueue_request(
        source_message_id="second",
        session_id="due-session",
        principal_id=provider_session_ref.principal_id,
        project_ref=provider_session_ref.project_ref,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert second.outcome == "accepted"
    assert second.row is not None
    assert second.row.flush_generation == first_token.generation + 1
    assert store.claim_due(lease_owner="blocked", now="2026-01-01T00:00:03.000Z") is None
    assert store.list_due_flush_sessions(now="2026-01-01T00:00:03.000Z") == ()
    assert store.list_due_flush_sessions(now="2026-01-01T00:00:32.000Z") == (
        provider_session_ref,
    )


def test_permanent_flush_rejection_does_not_schedule_a_retry(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "permanent")
    token = _flush_claim(store, provider_session_ref)

    assert store.record_flush_verdict(
        token,
        FlushRejected("permanent", "INVALID_INPUT", server_fault=False, retryable=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert state.flush_state == "not_due"
    assert store.list_due_flush_sessions(now="2026-01-01T00:00:03.000Z") == ()


def test_retryable_flush_rejections_back_off_then_fence_the_session(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "bounded-retry")

    token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        token,
        FlushRejected("first", "CONFLICT", server_fault=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert (state.flush_state, state.flush_retry_count, state.next_attempt_at) == (
        "due",
        1,
        "2026-01-01T00:00:32.000Z",
    )
    assert store.list_due_flush_sessions(now="2026-01-01T00:00:31.000Z") == ()
    assert store.list_due_flush_sessions(now="2026-01-01T00:00:32.000Z") == (
        provider_session_ref,
    )

    token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        token,
        FlushRejected("second", "CONFLICT", server_fault=False),
        now="2026-01-01T00:02:00.000Z",
    ) == 1
    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert (state.flush_state, state.flush_retry_count, state.next_attempt_at) == (
        "due",
        2,
        "2026-01-01T00:04:00.000Z",
    )

    token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        token,
        FlushRejected("third", "CONFLICT", server_fault=False),
        now="2026-01-01T00:05:00.000Z",
    ) == 1
    state = store.get_session_flush_state(provider_session_ref)
    assert state is not None
    assert (state.flush_state, state.flush_retry_count, state.next_attempt_at) == (
        "manual_required",
        0,
        None,
    )
    assert store.ensure_meta().last_error == "memory_processing_failed"
    assert len(store.list_flush_settlements(provider_session_ref)) == MAX_FLUSH_RETRY_ATTEMPTS
    assert store.list_due_flush_sessions(now="2026-01-01T00:06:00.000Z") == ()


def test_flush_verdict_updates_only_its_exact_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "old-rejection", session_ref="generation-session")
    first_token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        first_token,
        FlushRejected("old", "INVALID_INPUT", server_fault=False, retryable=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1

    _deliver(store, "generation-success", session_ref="generation-session")
    success_token = _flush_claim(store, provider_session_ref)
    assert success_token.generation == first_token.generation
    assert store.record_flush_verdict(
        success_token,
        FlushSucceeded("success", "extracted"),
        now="2026-01-01T00:00:03.000Z",
    ) == 1

    _deliver(store, "current-rejection", session_ref="generation-session")
    current_token = _flush_claim(store, provider_session_ref)
    assert current_token.generation == first_token.generation + 1
    assert store.record_flush_verdict(
        current_token,
        FlushRejected("current", "CONFLICT", server_fault=False),
        now="2026-01-01T00:00:04.000Z",
    ) == 1

    rows = {row.source_message_digest: row for row in store.list_queue_rows()}
    meta = store.ensure_meta()
    assert rows[_keyed_digest(meta.scope_key, "old-rejection")].flush_observation == "rejected"
    assert rows[_keyed_digest(meta.scope_key, "generation-success")].flush_observation == "succeeded"
    assert rows[_keyed_digest(meta.scope_key, "current-rejection")].flush_observation == "rejected"
    assert rows[_keyed_digest(meta.scope_key, "current-rejection")].flush_generation == current_token.generation
    assert [record.generation for record in store.list_flush_settlements(provider_session_ref)] == [0, 0, 1]


def test_compaction_preserves_in_flight_flush_evidence(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "in-flight-compaction")
    _flush_claim(store, provider_session_ref)
    old = datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE memory_capture_queue SET completed_at = ? WHERE source_message_digest = 'in-flight-compaction'",
            (old.isoformat().replace("+00:00", "Z"),),
        )

    reference = old + TERMINAL_TOMBSTONE_RETENTION + timedelta(seconds=1)
    assert store.compact_terminal_tombstones(now=reference) == 0
    row = _row_for_source(store, "in-flight-compaction")
    assert row is not None
    assert row.flush_observation == "in_flight"


def test_compaction_preserves_due_flush_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "due-compaction")
    token = _flush_claim(store, provider_session_ref)
    assert store.record_flush_verdict(
        token,
        FlushRejected("retry", "CONFLICT", server_fault=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    monkeypatch.setattr("core.memory.store.TERMINAL_TOMBSTONE_LIMIT", 0)

    old = datetime(2026, 1, 1, tzinfo=UTC)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            """
            UPDATE memory_capture_queue
            SET completed_at = ?, flush_observed_at = ?
            WHERE source_message_digest = 'due-compaction'
            """,
            (old.isoformat().replace("+00:00", "Z"), old.isoformat().replace("+00:00", "Z")),
        )

    reference = old + TERMINAL_TOMBSTONE_RETENTION + timedelta(seconds=1)
    assert store.compact_terminal_tombstones(now=reference) == 0
    row = _row_for_source(store, "due-compaction")
    assert row is not None
    assert row.flush_observation == "rejected"
    assert store.get_session_flush_state(provider_session_ref).flush_state == "due"


def test_malformed_flush_success_is_recorded_as_manual_required(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    session_ref = _deliver(store, "malformed-flush")
    token = _flush_claim(store, session_ref)

    assert store.record_flush_verdict(
        token,
        FlushSucceeded(request_id="partial-flush", status=None),
        now="2026-01-01T00:00:03.000Z",
    ) == 1
    row = _row_for_source(store, "malformed-flush")
    state = store.get_session_flush_state(row.provider_session_ref)
    assert state is not None
    assert state.flush_state == "manual_required"
    settlement = store.list_flush_settlements(row.provider_session_ref)[0]
    assert (settlement.outcome, settlement.request_id) == (
        "manual_required",
        "partial-flush",
    )


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


def test_ambiguous_add_is_terminal_manual_required_and_fences_its_session(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    accepted = _enqueue(store, "ambiguous")
    assert accepted.row is not None
    row = store.claim_due(lease_owner="boot", now="2026-01-01T00:00:00.000Z")
    assert row is not None

    result = store.settle(
        row,
        AmbiguousAdd(add_request_id="provider-request", error="memory_provider_timeout"),
        lease_owner="boot",
        now=_dt("2026-01-01T00:00:01.000Z"),
    )

    assert result == SettleResult(settled=True, state="manual_required", attempts=None)
    retained = _row_for_source(store, "ambiguous")
    assert retained.state == "pending"
    assert retained.payload_text == "queued payload"
    assert retained.last_error == "memory_provider_timeout"
    state = store.get_session_flush_state(retained.provider_session_ref)
    assert state is not None
    assert (state.flush_state, state.fence_epoch, state.fence_owner) == (
        "manual_required",
        1,
        "manual-required",
    )
    settlement = store.list_flush_settlements(retained.provider_session_ref)
    assert len(settlement) == 1
    assert settlement[0].outcome == "manual_required"
    assert settlement[0].operation_kind == "add"
    assert store.claim_due(lease_owner="next-boot", now="2026-01-01T00:01:00.000Z") is None


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
    _flush_claim(store, in_flight_session)

    recovery = store.recover_after_boot(
        lease_owner="boot",
        clock=lambda: _dt("2026-01-01T00:00:05.000Z"),
    )

    assert recovery.interrupted_flushes == 1
    assert _row_for_source(store, "in-flight").flush_observation == "unknown"
    assert store.ensure_meta().last_error == "memory_processing_failed"
    # Sessions are listed only after interrupted flushes have been resolved;
    # recover_after_boot owns that ordering.
    assert recovery.not_attempted_sessions == (not_attempted_session,)
    assert store.get_session_flush_state(in_flight_session).flush_state == "manual_required"


def test_boot_recovery_samples_its_clock_after_reclaiming_leases(tmp_path: Path) -> None:
    """Reclamation can block on SQLite contention; the flush stamp must postdate it.

    A backdated `flush_observed_at` reorders the `ORDER BY
    COALESCE(flush_observed_at, ...)` history, so the sampling point is part of
    this method's contract rather than a caller's detail.
    """

    store = MemoryStore(_store_path(tmp_path / "recovery-clock-order"))
    in_flight_session = _deliver(store, "in-flight", session_ref="in-flight-session")
    _flush_claim(store, in_flight_session)
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
    assert observed_states == ["pending"], "the clock was sampled before leases were fenced"
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
    assert store.get_session_flush_state(reclaimed.provider_session_ref).flush_state == "manual_required"
    assert store.claim_due(lease_owner="new-boot", now="2026-01-01T00:00:03.000Z") is None

    before = store.ensure_meta()
    clearing = store.begin_clear()
    assert clearing.epoch == before.epoch + 1
    assert clearing.clear_in_progress is True
    completed = store.finish_clear()
    assert completed.clear_in_progress is False
    assert completed.epoch == clearing.epoch
    assert store.list_queue_rows() == ()


def test_reclaim_processing_settlement_uses_pinned_generation(tmp_path: Path) -> None:
    store = MemoryStore(_store_path(tmp_path))
    provider_session_ref = _deliver(store, "first", session_ref="recovered-next-generation")
    token = _flush_claim(store, provider_session_ref)
    accepted = store.enqueue_request(
        source_message_id="second",
        session_id="recovered-next-generation",
        principal_id="u-11111111111111111111111111111111",
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="queued payload",
        occurred_at_ms=1_001,
        max_provider_timestamp_ms=4_102_444_800_000,
    )
    assert accepted.row is not None
    assert accepted.row.flush_generation == token.generation + 1
    assert store.record_flush_verdict(
        token,
        FlushRejected("rejected", "INVALID_INPUT", server_fault=False, retryable=False),
        now="2026-01-01T00:00:02.000Z",
    ) == 1
    claimed = store.claim_due(lease_owner="old-boot", now="2026-01-01T00:00:03.000Z")
    assert claimed is not None
    assert claimed.flush_generation == token.generation + 1

    assert store.recover_after_boot(
        lease_owner="new-boot",
        clock=lambda: _dt("2026-01-01T00:00:04.000Z"),
    ).reclaimed == 1

    settlement = next(
        item
        for item in store.list_flush_settlements(provider_session_ref)
        if item.operation_id == f"recovered-add-{claimed.source_message_digest}"
    )
    assert (settlement.generation, settlement.outcome) == (
        claimed.flush_generation,
        "manual_required",
    )


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
