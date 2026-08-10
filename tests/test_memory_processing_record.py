from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.memory.processing_record as processing_record_module
import core.memory.snapshot as snapshot_module
from config.v2_config import MemoryConfig
from core.memory.everos import ProviderHealthSnapshot
from core.memory.maintenance import MaintenanceObservation, MaintenanceResult
from core.memory.processing_record import (
    FailureLogObservation,
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
        self.maintenance_observation = MaintenanceObservation(None, None, True)
        self.operator_refs: list[str | None] = []
        self.failure_error: Exception | None = None
        self.maintenance_error: Exception | None = None
        self.maintenance_observation_error: Exception | None = None

    async def observe_maintenance(
        self,
        operator_ref: str | None,
    ) -> MaintenanceObservation:
        self.operator_refs.append(operator_ref)
        if self.maintenance_observation_error is not None:
            raise self.maintenance_observation_error
        return self.maintenance_observation

    async def resolve_operator(self, user_key: str) -> str:
        return f"u-{user_key}"

    async def observe_health(
        self,
        maintenance_reason: str | None,
    ) -> RuntimeHealthObservation:
        if maintenance_reason is not None:
            return RuntimeHealthObservation(None, maintenance_reason)
        return self.health

    async def failure_log(
        self,
        maintenance_reason: str | None,
    ) -> FailureLogObservation:
        if self.failure_error is not None:
            raise self.failure_error
        if maintenance_reason is not None:
            return FailureLogObservation((), maintenance_reason)
        return FailureLogObservation(self.failures)

    def recorder_health(self) -> dict[str, str | None]:
        return self.recorder

    async def observe_sources(
        self,
        maintenance_reason: str | None,
    ) -> ProcessingSourceObservations:
        if maintenance_reason is not None:
            unavailable = SourceObservation(
                "unavailable",
                reason=maintenance_reason,
            )
            return ProcessingSourceObservations(
                everos=unavailable,
                capture=unavailable,
                calls=unavailable,
            )
        return self.sources

    async def maintenance_payload(
        self,
        operator_ref: str | None,
        _observation: MaintenanceObservation,
    ) -> MaintenanceResult:
        if self.maintenance_error is not None:
            raise self.maintenance_error
        return self.maintenance

    def port(self) -> MemoryProcessingRecordPort:
        return MemoryProcessingRecordPort(
            resolve_operator=self.resolve_operator,
            observe_maintenance=self.observe_maintenance,
            observe_health=self.observe_health,
            failure_log=self.failure_log,
            recorder_health=self.recorder_health,
            observe_sources=self.observe_sources,
            maintenance=self.maintenance_payload,
        )


class _ConcurrentRuntimeObservations(_RuntimeObservations):
    def __init__(self) -> None:
        super().__init__()
        self._independent_reads = 0
        self._independent_reads_started = asyncio.Event()

    def _mark_independent_read(self) -> None:
        self._independent_reads += 1
        if self._independent_reads == 3:
            self._independent_reads_started.set()

    async def observe_health(
        self,
        _maintenance_reason: str | None,
    ) -> RuntimeHealthObservation:
        await self._independent_reads_started.wait()
        self.recorder = {"state": "degraded", "reason": "writer_failures"}
        return RuntimeHealthObservation(_health(self.recorder))

    async def failure_log(
        self,
        maintenance_reason: str | None,
    ) -> FailureLogObservation:
        self._mark_independent_read()
        return await super().failure_log(maintenance_reason)

    async def observe_sources(
        self,
        maintenance_reason: str | None,
    ) -> ProcessingSourceObservations:
        self._mark_independent_read()
        return await super().observe_sources(maintenance_reason)

    async def maintenance_payload(
        self,
        operator_ref: str | None,
        observation: MaintenanceObservation,
    ) -> MaintenanceResult:
        self._mark_independent_read()
        return await super().maintenance_payload(operator_ref, observation)


class _OverlappingRecorderObservations(_RuntimeObservations):
    def __init__(self) -> None:
        super().__init__()
        self._health_reads = 0
        self._failure_reads = 0
        self.first_health_read = asyncio.Event()
        self.release_first_failure_read = asyncio.Event()

    async def observe_health(
        self,
        _maintenance_reason: str | None,
    ) -> RuntimeHealthObservation:
        self._health_reads += 1
        if self._health_reads == 1:
            recorder = {"state": "degraded", "reason": "writer_failures"}
            self.recorder = recorder
            self.first_health_read.set()
            return RuntimeHealthObservation(_health(recorder))
        recorder = {"state": "active", "reason": None}
        self.recorder = recorder
        return RuntimeHealthObservation(_health(recorder))

    async def failure_log(
        self,
        maintenance_reason: str | None,
    ) -> FailureLogObservation:
        self._failure_reads += 1
        if self._failure_reads == 1:
            await self.release_first_failure_read.wait()
        return await super().failure_log(maintenance_reason)


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

    current = await record.read("record")
    observations.health = RuntimeHealthObservation(
        snapshot=None,
        unavailable_reason="memory_provider_timeout",
    )
    stale = await record.read("record")

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
    first = await record.read("record")
    second = await record.read("record")
    observations.recorder = {"state": "active", "reason": None}
    recovered = await record.read("record")
    observations.recorder = {"state": "degraded", "reason": "writer_failures"}
    next_episode = await record.read("record")
    observations.recorder = {"state": "degraded", "reason": "call_log_corrupt"}
    changed_reason = await record.read("record")

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

    summary = await MemoryProcessingRecord(observations.port()).read(
        "record",
        operator_ref="u-operator",
    )

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
async def test_verified_identity_lookup_is_inside_deadline_and_fails_closed() -> None:
    observations = _RuntimeObservations()

    async def blocked_operator_lookup(_user_key: str) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    port = replace(
        observations.port(),
        resolve_operator=blocked_operator_lookup,
    )
    deadline = asyncio.get_running_loop().time() + 0.01

    summary = await asyncio.wait_for(
        MemoryProcessingRecord(port).read(
            "record",
            verified_user_key="avibe:remote:subject",
            deadline=deadline,
        ),
        timeout=0.2,
    )

    assert summary.status == "ok"
    assert observations.operator_refs == []
    assert summary.maintenance.can_clear is False


@pytest.mark.asyncio
async def test_fenced_anomaly_read_reports_maintenance_reason() -> None:
    observations = _RuntimeObservations()
    observations.maintenance_observation = MaintenanceObservation(
        "busy",
        None,
        False,
    )

    summary = await MemoryProcessingRecord(observations.port()).read("record")

    assert summary.anomalies.source == SourceObservation(
        "unavailable",
        reason="busy",
    )
    assert summary.anomalies.items == ()


@pytest.mark.asyncio
async def test_maintenance_observation_failure_marks_only_maintenance_unavailable(
) -> None:
    observations = _RuntimeObservations()
    observations.maintenance_observation_error = OSError("journal unavailable")
    observations.maintenance = MaintenanceResult(False, False, None)

    summary = await MemoryProcessingRecord(observations.port()).read("record")

    assert summary.runtime.source.reason == "memory_store_unavailable"
    assert summary.sources.everos.reason == "memory_store_unavailable"
    assert summary.anomalies.source.reason == "memory_store_unavailable"
    assert summary.maintenance.source == SourceObservation(
        "unavailable",
        reason="memory_store_unavailable",
    )
    assert summary.maintenance.data_exists is False
    assert summary.maintenance.can_clear is False


@pytest.mark.asyncio
async def test_overlapping_composite_reads_keep_their_own_recorder_episode() -> None:
    observations = _OverlappingRecorderObservations()
    record = MemoryProcessingRecord(observations.port())

    first_reading = asyncio.create_task(record.read("record"))
    await asyncio.wait_for(observations.first_health_read.wait(), timeout=0.2)
    second = await asyncio.wait_for(record.read("record"), timeout=0.2)
    observations.release_first_failure_read.set()
    first = await asyncio.wait_for(first_reading, timeout=0.2)

    assert first.runtime.health is not None
    assert first.runtime.health.recorder["state"] == "degraded"
    assert [item.kind for item in first.anomalies.items] == [
        "recorder_degraded"
    ]
    assert second.runtime.health is not None
    assert second.runtime.health.recorder["state"] == "active"
    assert second.anomalies.items == ()


@pytest.mark.asyncio
async def test_composite_reads_sources_concurrently_then_merges_recorder_episode() -> None:
    observations = _ConcurrentRuntimeObservations()
    record = MemoryProcessingRecord(observations.port())

    summary = await asyncio.wait_for(record.read("record"), timeout=0.2)

    assert summary.runtime.health is not None
    assert summary.runtime.health.recorder == {
        "state": "degraded",
        "reason": "writer_failures",
    }
    assert [item.kind for item in summary.anomalies.items] == [
        "recorder_degraded"
    ]


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


@pytest.mark.asyncio
async def test_failure_compatibility_projection_skips_health_and_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    async def unexpected_read() -> None:
        raise AssertionError("compatibility failure reads must stay narrow")

    monkeypatch.setattr(runtime._processing_record, "_read_health", unexpected_read)
    monkeypatch.setattr(runtime._processing_record, "_read_sources", unexpected_read)

    result = await asyncio.wait_for(
        runtime.failure_log_payload(operator_ref="u-operator"),
        timeout=1,
    )

    assert result == {"status": "ok", "items": [], "recovery": None}
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_propagates_recorder_transition_time_at_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)

    runtime._update_recorder_health(
        {"state": "degraded", "reason": "writer_failures"},
        observed_at="transition-1",
    )
    first = await runtime.failure_log_payload()
    runtime._update_recorder_health(
        {"state": "active", "reason": None},
        observed_at="recovered",
    )
    runtime._update_recorder_health(
        {"state": "degraded", "reason": "writer_failures"},
        observed_at="transition-2",
    )
    second = await runtime.failure_log_payload()

    assert first["items"][0]["occurred_at"] == "transition-1"
    assert second["items"][0]["occurred_at"] == "transition-2"
    assert second["items"][0]["id"] != first["items"][0]["id"]
    await runtime.close()


@pytest.mark.asyncio
async def test_disabled_runtime_status_is_authoritative_when_store_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def unavailable_store():
        raise OSError("test store unavailable")

    monkeypatch.setattr("core.memory.runtime.MemoryStore", unavailable_store)
    runtime = MemoryRuntime(MemoryConfig(enabled=False), effective_home=tmp_path)

    assert runtime.available is False
    assert await runtime.status_payload() == {
        "status": "ok",
        "source": {
            "status": "unavailable",
            "observed_at": None,
            "reason": "memory_disabled",
        },
        "health": None,
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_health_probe_releases_reconcile_lock_and_discards_stale_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    original_process = SimpleNamespace(running=True)
    runtime._process = original_process
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_health() -> ProviderHealthSnapshot:
        probe_entered.set()
        await release_probe.wait()
        return _health({"state": "degraded", "reason": "writer_failures"})

    monkeypatch.setattr(runtime._provider, "health_snapshot", blocked_health)
    probing = asyncio.create_task(runtime._processing_record_health(None))
    await asyncio.wait_for(probe_entered.wait(), timeout=0.2)

    async with asyncio.timeout(0.2):
        async with runtime._reconcile_lock:
            runtime._process = SimpleNamespace(running=True)
    release_probe.set()
    observation = await probing

    assert observation == RuntimeHealthObservation(
        snapshot=None,
        unavailable_reason="memory_sidecar_unavailable",
    )
    assert runtime._recorder_health == {"state": "disabled", "reason": None}
    runtime._process = None
    await runtime.close()


@pytest.mark.asyncio
async def test_health_probe_returns_closed_without_waiting_for_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    runtime._process = SimpleNamespace(running=True)
    provider_called = False

    async def unexpected_health() -> ProviderHealthSnapshot:
        nonlocal provider_called
        provider_called = True
        return _health({"state": "active", "reason": None})

    monkeypatch.setattr(runtime._provider, "health_snapshot", unexpected_health)
    async with runtime._reconcile_lock:
        observation = await asyncio.wait_for(
            runtime._processing_record_health(None),
            timeout=0.1,
        )

    assert observation.snapshot is None
    assert observation.unavailable_reason == "busy"
    assert provider_called is False
    runtime._process = None
    await runtime.close()


@pytest.mark.asyncio
async def test_health_probe_discards_result_after_same_process_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    runtime._process = SimpleNamespace(running=True)
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_health() -> ProviderHealthSnapshot:
        probe_entered.set()
        await release_probe.wait()
        return _health({"state": "active", "reason": None})

    monkeypatch.setattr(runtime._provider, "health_snapshot", blocked_health)
    probing = asyncio.create_task(runtime._processing_record_health(None))
    await asyncio.wait_for(probe_entered.wait(), timeout=0.2)
    async with runtime._reconcile_lock:
        pass
    release_probe.set()

    observation = await probing

    assert observation == RuntimeHealthObservation(
        snapshot=None,
        unavailable_reason="memory_sidecar_unavailable",
    )
    runtime._process = None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled",
    (False, True),
    ids=("disabled", "sidecar-stopped"),
)
async def test_processing_sources_read_local_history_without_a_running_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    enabled: bool,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_reads = 0

    class Reader:
        def source_observation(self) -> ProcessingSourceObservations:
            nonlocal source_reads
            source_reads += 1
            return _sources()

    runtime = MemoryRuntime(
        MemoryConfig(enabled=enabled),
        effective_home=tmp_path,
        insight_reader=Reader(),
    )

    sources = await runtime._processing_record_sources(None)

    assert sources == _sources()
    assert source_reads == 1
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ("reconcile", "root"))
async def test_processing_anomalies_return_busy_without_waiting_for_fences(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fence: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=False), effective_home=tmp_path)
    failure_log_reads = 0

    def failure_log(*, limit: int) -> tuple[MemoryFailureLogEntry, ...]:
        nonlocal failure_log_reads
        failure_log_reads += 1
        assert limit == 50
        return ()

    monkeypatch.setattr(runtime._store, "failure_log", failure_log)
    lock = (
        runtime._reconcile_lock
        if fence == "reconcile"
        else runtime.module._root_lifecycle_lock()
    )

    async with lock:
        blocked = await asyncio.wait_for(
            runtime._processing_record_failure_log(None),
            timeout=0.1,
        )

    assert blocked == FailureLogObservation((), "busy")
    assert failure_log_reads == 0

    available = await asyncio.wait_for(
        runtime._processing_record_failure_log(None),
        timeout=0.1,
    )

    assert available == FailureLogObservation(())
    assert failure_log_reads == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_processing_anomalies_discard_read_after_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=False), effective_home=tmp_path)
    failure_log_started = threading.Event()
    release_failure_log = threading.Event()

    def failure_log(*, limit: int) -> tuple[MemoryFailureLogEntry, ...]:
        assert limit == 50
        failure_log_started.set()
        assert release_failure_log.wait(2)
        return ()

    monkeypatch.setattr(runtime._store, "failure_log", failure_log)
    reading = asyncio.create_task(runtime._processing_record_failure_log(None))
    try:
        assert await asyncio.to_thread(failure_log_started.wait, 1)
        runtime._advance_processing_lifecycle()
        release_failure_log.set()
        observation = await asyncio.wait_for(reading, timeout=1)
    finally:
        release_failure_log.set()
        await asyncio.gather(reading, return_exceptions=True)

    assert observation == FailureLogObservation((), "busy")
    assert runtime.module._root_lifecycle_lock().locked() is False
    await runtime.close()


@pytest.mark.asyncio
async def test_processing_sources_return_busy_without_waiting_for_lifecycle_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_reads = 0

    class Reader:
        def source_observation(self) -> ProcessingSourceObservations:
            nonlocal source_reads
            source_reads += 1
            return _sources()

    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        effective_home=tmp_path,
        insight_reader=Reader(),
    )
    runtime._process = SimpleNamespace(running=True)

    async with runtime._reconcile_lock, runtime.module._lifecycle_lock:
        sources = await asyncio.wait_for(
            runtime._processing_record_sources(None),
            timeout=0.1,
        )

    assert source_reads == 0
    assert {source.status for source in (
        sources.everos,
        sources.capture,
        sources.calls,
    )} == {"unavailable"}
    assert {source.reason for source in (
        sources.everos,
        sources.capture,
        sources.calls,
    )} == {"busy"}
    runtime._process = None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transition", "expected_reason"),
    [
        ("generation", "busy"),
        ("maintenance", "busy"),
    ],
)
async def test_processing_sources_discard_stale_lifecycle_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transition: str,
    expected_reason: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    source_read_started = threading.Event()
    release_source_read = threading.Event()

    class Reader:
        def source_observation(self) -> ProcessingSourceObservations:
            source_read_started.set()
            assert release_source_read.wait(2)
            return _sources()

    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        effective_home=tmp_path,
        insight_reader=Reader(),
    )
    original_process = SimpleNamespace(running=True)
    runtime._process = original_process
    reading = asyncio.create_task(runtime._processing_record_sources(None))
    try:
        assert await asyncio.to_thread(source_read_started.wait, 1)
        if transition == "generation":
            runtime._advance_processing_lifecycle()
        else:
            runtime._enter_maintenance()
        release_source_read.set()
        sources = await asyncio.wait_for(reading, timeout=1)
    finally:
        release_source_read.set()
        await asyncio.gather(reading, return_exceptions=True)
        if transition == "maintenance":
            runtime._leave_maintenance()

    assert {source.status for source in (
        sources.everos,
        sources.capture,
        sources.calls,
    )} == {"unavailable"}
    assert {source.reason for source in (
        sources.everos,
        sources.capture,
        sources.calls,
    )} == {expected_reason}
    runtime._process = None
    await runtime.close()


@pytest.mark.asyncio
async def test_status_projection_never_reads_maintenance_journals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=False), effective_home=tmp_path)

    async def unexpected_read(*_args: object) -> object:
        raise AssertionError("status compatibility reads must stay runtime-only")

    runtime._processing_record._runtime = replace(
        runtime._processing_record._runtime,
        resolve_operator=unexpected_read,
        observe_maintenance=unexpected_read,
        failure_log=unexpected_read,
        observe_sources=unexpected_read,
        maintenance=unexpected_read,
    )

    payload = await runtime.status_payload()

    assert payload["source"]["reason"] == "memory_disabled"
    await runtime.close()


@pytest.mark.asyncio
async def test_processing_record_keeps_event_loop_responsive_during_journal_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    maintenance = runtime._maintenance
    assert maintenance is not None
    restore_journal = maintenance._backup_restore_journal
    assert restore_journal is not None
    release_journal = threading.Event()
    watchdog_released = threading.Event()
    heartbeat = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    journal_reads = 0
    original_get_open_operation = restore_journal.get_open_operation

    def blocking_get_open_operation():
        nonlocal journal_reads
        journal_reads += 1
        event_loop.call_soon_threadsafe(heartbeat.set)
        assert release_journal.wait(1)
        return original_get_open_operation()

    def release_from_watchdog() -> None:
        watchdog_released.set()
        release_journal.set()

    monkeypatch.setattr(
        restore_journal,
        "get_open_operation",
        blocking_get_open_operation,
    )
    watchdog = threading.Timer(0.5, release_from_watchdog)
    watchdog.start()
    reading = asyncio.create_task(runtime.processing_record_payload())
    try:
        await asyncio.wait_for(heartbeat.wait(), timeout=1)
        assert watchdog_released.is_set() is False
        release_journal.set()
        await asyncio.wait_for(reading, timeout=1)
        assert journal_reads == 2
    finally:
        release_journal.set()
        watchdog.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_processing_sources_distinguish_busy_clear_from_failed_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(enabled=True), effective_home=tmp_path)
    maintenance = runtime._maintenance
    assert maintenance is not None
    journal = maintenance._clear_journal
    assert journal is not None
    maintenance._backup_active = True
    assert maintenance.observation_block_reason() == "busy"
    maintenance._backup_active = False
    restore_journal = maintenance._backup_restore_journal
    assert restore_journal is not None

    def unavailable_clear_journal():
        raise OSError("clear journal unavailable")

    with monkeypatch.context() as restore_patch:
        restore_patch.setattr(
            restore_journal,
            "get_open_operation",
            lambda: object(),
        )
        assert maintenance.observation_block_reason() == "busy"
        restore_patch.setattr(
            journal,
            "get_open_operation",
            unavailable_clear_journal,
        )
        observation = await runtime._processing_record_maintenance_observation(None)
        assert observation.block_reason == "busy"
    operation = journal.start(
        operation_id="processing-observation-clear",
        operator_ref="user:owner",
        pre_epoch=0,
        target_epoch=1,
    )

    busy_observation = await runtime._processing_record_maintenance_observation(None)
    busy_health = await runtime._processing_record_health(
        busy_observation.block_reason
    )
    busy_sources = await runtime._processing_record_sources(
        busy_observation.block_reason
    )
    recovery = journal.mark_recovery_needed(
        operation.operation_id,
        expected_revision=operation.revision,
        execution_token=operation.execution_token,
    )
    failed_observation = await runtime._processing_record_maintenance_observation(None)
    failed_health = await runtime._processing_record_health(
        failed_observation.block_reason
    )
    failed_sources = await runtime._processing_record_sources(
        failed_observation.block_reason
    )

    assert busy_health.unavailable_reason == "busy"
    assert {source.reason for source in (
        busy_sources.everos,
        busy_sources.capture,
        busy_sources.calls,
    )} == {"busy"}
    assert recovery.closed_error == "memory_clear_failed"
    assert failed_health.unavailable_reason == "memory_clear_failed"
    assert {source.reason for source in (
        failed_sources.everos,
        failed_sources.capture,
        failed_sources.calls,
    )} == {"memory_clear_failed"}
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
