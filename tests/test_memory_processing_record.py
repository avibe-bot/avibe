from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import core.memory.processing_record as processing_record_module
import core.memory.snapshot as snapshot_module
from config.v2_config import MemoryConfig
from core.memory.everos import ProviderHealthSnapshot
from core.memory.maintenance import MaintenanceResult
from core.memory.processing_record import (
    MemoryProcessingRecord,
    MemoryProcessingRecordPort,
    ProcessingSourceObservations,
    RuntimeHealthObservation,
    SourceObservation,
)
from core.memory.runtime import MemoryRuntime
from core.memory.store import Delivered
from core.memory.types import MemoryFailureLogEntry


def _health(recorder: dict[str, str | None]) -> ProviderHealthSnapshot:
    return ProviderHealthSnapshot(
        status="ok",
        version="1.2.3",
        capabilities={"llm": True},
        disabled_features=(),
        cascade=None,
        recorder=recorder,
    )


def _sources() -> ProcessingSourceObservations:
    observed = SourceObservation(
        "available",
        observed_at="2026-08-09T00:00:00.000Z",
    )
    return ProcessingSourceObservations(
        everos=observed,
        capture=observed,
        calls=observed,
    )


class _RuntimeObservations:
    def __init__(self) -> None:
        self.health = RuntimeHealthObservation(
            _health({"state": "active", "reason": None})
        )
        self.failures: tuple[MemoryFailureLogEntry, ...] = ()
        self.recorder: dict[str, str | None] = {
            "state": "active",
            "reason": None,
        }
        self.sources = _sources()
        self.maintenance = MaintenanceResult(False, True, None)
        self.operator_refs: list[str | None] = []
        self.failure_error: Exception | None = None
        self.maintenance_error: Exception | None = None

    async def observe_health(self) -> RuntimeHealthObservation:
        return self.health

    async def failure_log(self) -> tuple[MemoryFailureLogEntry, ...]:
        if self.failure_error is not None:
            raise self.failure_error
        return self.failures

    def recorder_health(self) -> dict[str, str | None]:
        return self.recorder

    async def observe_sources(self) -> ProcessingSourceObservations:
        return self.sources

    async def maintenance_payload(
        self,
        operator_ref: str | None,
    ) -> MaintenanceResult:
        self.operator_refs.append(operator_ref)
        if self.maintenance_error is not None:
            raise self.maintenance_error
        return self.maintenance

    def port(self) -> MemoryProcessingRecordPort:
        return MemoryProcessingRecordPort(
            observe_health=self.observe_health,
            failure_log=self.failure_log,
            recorder_health=self.recorder_health,
            observe_sources=self.observe_sources,
            maintenance=self.maintenance_payload,
        )


@pytest.mark.asyncio
async def test_health_snapshot_falls_back_only_to_its_own_stale_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _RuntimeObservations()
    record = MemoryProcessingRecord(observations.port())
    times = iter(
        ("current", "anomalies", "maintenance", "stale-anomalies", "stale-maintenance")
    )
    monkeypatch.setattr(
        processing_record_module,
        "_utc_observed_at",
        lambda: next(times),
    )

    current = await record.read(None)
    observations.health = RuntimeHealthObservation(
        snapshot=None,
        unavailable_reason="memory_provider_timeout",
    )
    stale = await record.read(None)

    assert current.runtime.source == SourceObservation("available", "current")
    assert stale.runtime.source == SourceObservation(
        "stale",
        "current",
        "memory_provider_timeout",
    )
    assert stale.runtime.health is current.runtime.health


@pytest.mark.asyncio
async def test_recorder_episode_is_stable_and_rotates_after_recovery_or_reason_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = _RuntimeObservations()
    observations.failures = (
        MemoryFailureLogEntry(
            id="ma_durable",
            kind="delivery_abandoned",
            occurred_at="durable",
        ),
    )
    record = MemoryProcessingRecord(observations.port())
    episode_ids = iter(("ma_episode_1", "ma_episode_2", "ma_episode_3"))
    times = iter(
        (
            "health-1", "anomalies-1", "maintenance-1",
            "health-2", "anomalies-2", "maintenance-2",
            "health-3", "anomalies-3", "maintenance-3",
            "health-4", "anomalies-4", "maintenance-4",
            "health-5", "anomalies-5", "maintenance-5",
        )
    )
    monkeypatch.setattr(
        processing_record_module,
        "_new_recorder_episode_id",
        lambda: next(episode_ids),
    )
    monkeypatch.setattr(
        processing_record_module,
        "_utc_observed_at",
        lambda: next(times),
    )

    observations.recorder = {"state": "degraded", "reason": "writer_failures"}
    first = await record.read(None)
    second = await record.read(None)
    observations.recorder = {"state": "active", "reason": None}
    recovered = await record.read(None)
    observations.recorder = {"state": "degraded", "reason": "writer_failures"}
    next_episode = await record.read(None)
    observations.recorder = {"state": "degraded", "reason": "call_log_corrupt"}
    changed_reason = await record.read(None)

    assert [item.id for item in first.anomalies.items] == [
        "ma_episode_1",
        "ma_durable",
    ]
    assert first.anomalies.items[0].occurred_at == "health-1"
    assert second.anomalies.items[0].id == "ma_episode_1"
    assert second.anomalies.items[0].occurred_at == "health-1"
    assert [item.id for item in recovered.anomalies.items] == ["ma_durable"]
    assert next_episode.anomalies.items[0].id == "ma_episode_2"
    assert changed_reason.anomalies.items[0].id == "ma_episode_3"


@pytest.mark.asyncio
async def test_sources_anomalies_and_maintenance_degrade_independently() -> None:
    observations = _RuntimeObservations()
    observations.sources = ProcessingSourceObservations(
        everos=SourceObservation("partial", reason="runs_malformed"),
        capture=SourceObservation("unavailable", reason="busy"),
        calls=SourceObservation("available", observed_at="calls-now"),
    )
    observations.failure_error = OSError("private failure detail")
    observations.maintenance_error = OSError("private maintenance detail")
    observations.recorder = {"state": "degraded", "reason": "writer_failures"}

    summary = await MemoryProcessingRecord(observations.port()).read("u-operator")

    assert summary.runtime.health is not None
    assert summary.sources.capture.reason == "busy"
    assert summary.sources.calls.status == "available"
    assert summary.anomalies.source == SourceObservation(
        "unavailable",
        reason="memory_store_unavailable",
    )
    assert [item.kind for item in summary.anomalies.items] == [
        "recorder_degraded"
    ]
    assert summary.maintenance.data_exists is True
    assert summary.maintenance.can_clear is False
    assert summary.maintenance.clear_recovery is None
    assert summary.maintenance.source.reason == "memory_store_unavailable"
    assert observations.operator_refs == ["u-operator"]
    assert "private" not in repr(summary)


@pytest.mark.asyncio
async def test_runtime_serializes_one_composite_and_compatibility_projections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    composite = await runtime.processing_record_payload(operator_ref="u-operator")
    status = await runtime.status_payload()
    failures = await runtime.failure_log_payload(operator_ref="u-operator")
    maintenance = await runtime.maintenance_payload(operator_ref="u-operator")

    assert set(composite) == {
        "status",
        "runtime",
        "sources",
        "anomalies",
        "maintenance",
    }
    assert set(composite["sources"]) == {"everos", "capture", "calls"}
    assert status == {"status": "ok", **composite["runtime"]}
    assert failures == {
        "status": "ok",
        "items": composite["anomalies"]["items"],
        "recovery": composite["maintenance"]["clear_recovery"],
    }
    assert maintenance == {
        "status": "ok",
        "data_exists": composite["maintenance"]["data_exists"],
        "can_clear": composite["maintenance"]["can_clear"],
        "clear_recovery": composite["maintenance"]["clear_recovery"],
    }
    assert "u-operator" not in repr(composite)
    await runtime.close()


def _enqueue(runtime: MemoryRuntime, source: str) -> None:
    result = runtime._store.enqueue_request(
        source_message_id=source,
        session_id="session",
        principal_id="u-11111111111111111111111111111111",
        project_ref="p-22222222222222222222222222222222",
        provenance="user_input",
        payload_text="private payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
    )
    assert result.outcome == "accepted"


@pytest.mark.asyncio
async def test_processing_record_does_not_compact_queue_during_clear_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr("core.memory.store.TERMINAL_TOMBSTONE_LIMIT", 0)
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "terminal-before-clear")
    row = runtime._store.claim_due(
        lease_owner="boot",
        now="2026-01-01T00:00:00.000Z",
    )
    assert row is not None
    assert runtime._store.settle(
        row,
        Delivered(add_request_id="add-terminal-before-clear"),
        lease_owner="boot",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ).settled

    snapshot_copied = threading.Event()
    release_snapshot = threading.Event()
    compaction_entered = threading.Event()
    queue_uri = runtime._store.path.absolute().as_uri() + "?mode=ro"
    original_connect = snapshot_module.sqlite3.connect
    original_compact = runtime._store._compact_terminal_tombstones_in_connection

    class BlockingQueueConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, *args, **kwargs):
            return self._connection.execute(*args, **kwargs)

        def backup(self, target: sqlite3.Connection) -> None:
            self._connection.backup(target)
            snapshot_copied.set()
            assert release_snapshot.wait(2)

        def close(self) -> None:
            self._connection.close()

    def blocking_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if database == queue_uri:
            return BlockingQueueConnection(connection)
        return connection

    def observed_compaction(connection, reference):
        compaction_entered.set()
        return original_compact(connection, reference)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", blocking_connect)
    monkeypatch.setattr(
        runtime._store,
        "_compact_terminal_tombstones_in_connection",
        observed_compaction,
    )
    maintenance = runtime._maintenance
    assert maintenance is not None
    clearing = asyncio.create_task(maintenance.clear(operator_ref="user:owner"))
    assert await asyncio.to_thread(snapshot_copied.wait, 1)
    runtime._update_recorder_health(
        {"state": "degraded", "reason": "call_log_corrupt"},
    )
    processing_record = await asyncio.wait_for(
        runtime.processing_record_payload(operator_ref="user:owner"),
        1,
    )
    compacted_during_snapshot = compaction_entered.is_set()
    release_snapshot.set()
    result = await clearing

    assert compacted_during_snapshot is False
    assert [item["kind"] for item in processing_record["anomalies"]["items"]] == [
        "recorder_degraded"
    ]
    assert result.status == "completed"
    assert maintenance._clear_journal.get_open_operation() is None
    await runtime.close()
