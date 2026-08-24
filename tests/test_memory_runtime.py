"""Focused runtime lifecycle tests for volatile Memory delivery."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from config.v2_config import MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.clear_intent import ClearSurface
from core.memory.everos import (
    FakeMemoryProvider,
    MemoryProviderFailure,
    ProviderHealthSnapshot,
)
from core.memory.operation_lock import MemoryOperationLease
from core.memory.process import FakeEverOSProcessFactory
from core.memory.processing_record import RuntimeHealthProjection, SourceObservation
from core.memory.runtime import MemoryConfig, MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import CaptureAccepted, CaptureRequest


def _runtime(tmp_path: Path) -> MemoryRuntime:
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite", effective_home=tmp_path)
    return MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )


@pytest.mark.asyncio
async def test_session_lifecycle_offers_without_waiting_for_capture(tmp_path: Path) -> None:
    """MEMORY-SEARCH-006: runtime forwards the raw session to a volatile barrier."""

    runtime = _runtime(tmp_path)
    offered: list[str | None] = []
    runtime.module.offer_barrier = lambda raw_session_id: (
        offered.append(raw_session_id) or "queued"
    )

    result = runtime.offer_barrier("session")

    assert result == "queued"
    assert offered == ["session"]
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_drops_volatile_work(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await runtime.close()
    assert runtime.closed


@pytest.mark.asyncio
async def test_runtime_close_cancels_pending_automatic_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-INDEP-003: shutdown cancels volatile recovery work."""

    runtime = _runtime(tmp_path)
    restart_entered = asyncio.Event()

    async def pending_restart() -> dict[str, object]:
        restart_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled restart resumed")

    monkeypatch.setattr(runtime, "_restart_once", pending_restart)
    await runtime._settle_ambiguous_provider_outcome(recover=True)
    restart = runtime._restart_task
    assert restart is not None
    await restart_entered.wait()

    await asyncio.wait_for(runtime.close(), timeout=1.0)

    assert restart.cancelled()
    assert runtime._restart_task is None
    assert runtime.closed


@pytest.mark.asyncio
async def test_ambiguous_recovery_waits_for_active_repair_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    repair_lease = MemoryOperationLease(tmp_path)
    repair_lease.acquire()
    release_repair = asyncio.Event()

    async def active_repair() -> dict[str, object]:
        try:
            await release_repair.wait()
            return {"ok": True}
        finally:
            repair_lease.release()

    repair = asyncio.create_task(active_repair())
    runtime._repair_task = repair
    restarted = asyncio.Event()

    async def restart_locked() -> dict[str, object]:
        restarted.set()
        return {"ok": True}

    monkeypatch.setattr(runtime, "_restart_locked", restart_locked)
    await runtime._settle_ambiguous_provider_outcome(recover=True)
    restart = runtime._restart_task
    assert restart is not None
    await asyncio.sleep(0)
    assert not restarted.is_set()

    release_repair.set()
    await asyncio.wait_for(restarted.wait(), timeout=1.0)
    await restart
    runtime._repair_task = None
    await runtime.close()


@pytest.mark.asyncio
async def test_clear_completion_rotates_volatile_duplicate_generation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    writer = runtime.module._writer
    reservation = writer.reserve("same-source")
    assert not isinstance(reservation, str)
    reservation.release()
    assert writer.reserve("same-source") == "duplicate"

    runtime._maintenance_runtime_port().restore_completed()

    replacement = writer.reserve("same-source")
    assert not isinstance(replacement, str)
    replacement.release()
    await runtime.close()


@pytest.mark.asyncio
async def test_clear_legacy_files_surface_removes_retired_provider_call_storage(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    legacy = tmp_path / "memory" / "call-log"
    legacy.mkdir(parents=True)
    (legacy / "call-log.db").write_bytes(b"retired")

    await runtime._delete_clear_surface(
        ClearSurface("legacy_files", "memory/call-log"),
        target_epoch=1,
    )

    assert not legacy.exists()
    await runtime.close()


@pytest.mark.asyncio
async def test_processing_record_uses_native_sources_without_call_log(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    projection = await runtime._processing_record_sources(None)

    assert projection.memcells.status == "unavailable"
    assert projection.runs.status == "unavailable"
    assert projection.semantic.status == "unavailable"
    assert not hasattr(runtime, "log_entries_payload")
    assert not hasattr(runtime, "log_unlinked_calls_payload")
    assert not hasattr(runtime, "_call_log_db_path")
    await runtime.close()


@pytest.mark.asyncio
async def test_capture_diagnostics_are_unavailable_without_delivery_history(
    tmp_path: Path,
) -> None:
    """MEMORY-SEARCH-014: absent durable history is unavailable, not empty."""

    runtime = _runtime(tmp_path)

    observation = await runtime._processing_record_failure_log(None)

    assert observation.items == ()
    assert observation.unavailable_reason == "memory_failure_history_unavailable"
    await runtime.close()


@pytest.mark.asyncio
async def test_disabled_attachment_intake_is_unavailable_in_every_readiness_projection(
    tmp_path: Path,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed",
            "embed-key",
        ),
        multimodal=MemoryEndpointConfig(
            "https://vision.example.test/v1",
            "vision",
            "vision-key",
        ),
    )
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, processing=processing),
        store=MemoryStore(
            tmp_path / "state" / "memory" / "memory.sqlite",
            effective_home=tmp_path,
        ),
        effective_home=tmp_path,
    )
    runtime.module._writer.disable_attachment_intake()

    async def healthy_status() -> RuntimeHealthProjection:
        return RuntimeHealthProjection(
            SourceObservation("available"),
            ProviderHealthSnapshot(
                status="ok",
                version="test",
                capabilities={"multimodal_llm": True, "parser": True},
                disabled_features=(),
                cascade={},
            ),
        )

    runtime._processing_record.read_status = healthy_status

    assert (await runtime.status_payload())["attachment_capture"] == {
        "status": "unavailable"
    }
    assert await runtime.attachment_capture_status() == "unavailable"
    assert runtime.attachment_capture_config_generation() is None
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_provider_root_cutover_keeps_capture_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=MemoryStore(
            tmp_path / "state" / "memory" / "memory.sqlite",
            effective_home=tmp_path,
        ),
        artifact_manager=FakeMemoryArtifactManager(python=Path(__file__)),
        effective_home=tmp_path,
    )

    async def probe_processing(_python: Path, _config: MemoryConfig) -> bool:
        return True

    def reject_provider_root(*_args: object) -> None:
        raise OSError("provider root is invalid")

    monkeypatch.setattr(runtime, "_probe_processing", probe_processing)
    monkeypatch.setattr(runtime._provider_root_owner, "ensure", reject_provider_root)
    runtime.module.pause_claims()

    result = await runtime._reconcile_locked(
        runtime._config,
        claims_already_paused=True,
    )

    assert result == {"ok": False, "error": "memory_clear_failed"}
    assert runtime.module._writer.reserve("later") == "disabled"
    await runtime.close()


@pytest.mark.asyncio
async def test_rejected_embedding_change_restores_previous_writer_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed-v1",
            "embed-key",
        ),
    )
    current = MemoryConfig(enabled=True, processing=processing)
    process_factory = FakeEverOSProcessFactory()
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    runtime = MemoryRuntime(
        current,
        store=store,
        artifact_manager=FakeMemoryArtifactManager(
            python=Path(__file__),
            root_format="everos-test",
            fingerprint="test-artifact",
        ),
        process_factory=process_factory,
        effective_home=tmp_path,
    )
    async with runtime.module.lifecycle():
        assert await runtime._reconcile_locked(current) == {
            "ok": True,
            "state": "ready",
        }

    add_entered = asyncio.Event()

    async def block_add(_capture) -> None:
        add_entered.set()
        await asyncio.Event().wait()

    runtime.module.replace_provider(FakeMemoryProvider(add_hook=block_add))
    principal = store.principal_for_user_key("slack:U123")
    assert await runtime.module.capture(
        CaptureRequest(
            source_message_id="source-in-flight",
            session_id="session-1",
            principal_id=principal,
            project_id="default",
            provenance="user_input",
            text="remember this",
            occurred_at_ms=1_000,
        )
    ) == CaptureAccepted()
    await add_entered.wait()

    async def reject_embedding_change(
        _current: MemoryConfig,
        _candidate: MemoryConfig,
    ) -> bool:
        return False

    monkeypatch.setattr(
        runtime,
        "_embedding_change_is_admissible",
        reject_embedding_change,
    )
    candidate = replace(
        current,
        processing=replace(
            processing,
            embedding=replace(processing.embedding, model="embed-v2"),
        ),
    )
    async with runtime.module.lifecycle():
        result = await runtime._reconcile_locked(candidate)

    assert result == {"ok": False, "error": "memory_clear_failed"}
    assert runtime._config == current
    assert runtime._runtime_error is None
    assert len(process_factory.supervised) == 2
    assert process_factory.supervised[0].stopped
    assert process_factory.supervised[1].running
    assert not runtime.module._writer.unavailable
    reservation = runtime.module._writer.reserve("after-rejection")
    assert not isinstance(reservation, str)
    reservation.release()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["reconcile", "restart"])
async def test_real_quiesce_timeout_defers_previous_writer_authority_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed",
            "embed-key",
        ),
    )
    current = MemoryConfig(enabled=True, processing=processing)
    process_factory = FakeEverOSProcessFactory()
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    runtime = MemoryRuntime(
        current,
        store=store,
        artifact_manager=FakeMemoryArtifactManager(
            python=Path(__file__),
            root_format="everos-test",
            fingerprint="test-artifact",
        ),
        process_factory=process_factory,
        effective_home=tmp_path,
    )

    async def probe_processing(_python: Path, _config: MemoryConfig) -> bool:
        return True

    monkeypatch.setattr(runtime, "_probe_processing", probe_processing)
    async with runtime.module.lifecycle():
        assert await runtime._reconcile_locked(current) == {
            "ok": True,
            "state": "ready",
        }

    add_entered = asyncio.Event()

    async def block_add(_capture) -> None:
        add_entered.set()
        await asyncio.Event().wait()

    runtime.module.replace_provider(FakeMemoryProvider(add_hook=block_add))
    principal = store.principal_for_user_key("slack:U123")
    assert await runtime.module.capture(
        CaptureRequest(
            source_message_id="source-in-flight",
            session_id="session-1",
            principal_id=principal,
            project_id="default",
            provenance="user_input",
            text="remember this",
            occurred_at_ms=1_000,
        )
    ) == CaptureAccepted()
    await add_entered.wait()

    recoveries: list[bool] = []
    finish_settlement = asyncio.Event()

    async def stop_then_settle(recover: bool) -> bool:
        recoveries.append(recover)
        await runtime._sidecar.stop()
        await finish_settlement.wait()
        return True

    runtime.module._writer._ambiguous_stop_reap = stop_then_settle
    original_quiesce = runtime.module.quiesce_claims

    async def short_quiesce(*, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        return await original_quiesce(timeout_seconds=0.001)

    monkeypatch.setattr(runtime.module, "quiesce_claims", short_quiesce)

    async def run_transition() -> dict[str, object]:
        if entrypoint == "restart":
            return await runtime.restart()
        async with runtime.module.lifecycle():
            return await runtime._reconcile_locked(current)

    recovery: asyncio.Task[dict[str, object]] | None = None
    try:
        result = await asyncio.wait_for(run_transition(), timeout=0.2)
        expected_error = (
            "memory_restart_failed" if entrypoint == "restart" else "memory_clear_failed"
        )
        assert result == {"ok": False, "error": expected_error}
        assert len(process_factory.supervised) == 1
        fenced = runtime.module._writer.reserve("while-cleanup-settles")
        assert fenced in ("disabled", "unavailable")
        recovery = runtime._restart_task
        assert recovery is not None
        assert not recovery.done()
    finally:
        finish_settlement.set()

    assert recovery is not None
    assert await asyncio.wait_for(asyncio.shield(recovery), timeout=1.0) == {
        "ok": True,
        "state": "ready",
    }
    assert recoveries == [False]
    assert len(process_factory.supervised) == 2
    assert process_factory.supervised[0].stopped
    assert process_factory.supervised[1].running
    assert not runtime.module._writer.unavailable
    reservation = runtime.module._writer.reserve("after-timeout")
    assert not isinstance(reservation, str)
    reservation.release()
    await runtime.close()


@pytest.mark.asyncio
async def test_ambiguous_add_restarts_settled_runtime_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed",
            "embed-key",
        ),
    )
    current = MemoryConfig(enabled=True, processing=processing)
    process_factory = FakeEverOSProcessFactory()
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    runtime = MemoryRuntime(
        current,
        store=store,
        artifact_manager=FakeMemoryArtifactManager(
            python=Path(__file__),
            root_format="everos-test",
            fingerprint="test-artifact",
        ),
        process_factory=process_factory,
        effective_home=tmp_path,
    )

    async def probe_processing(_python: Path, _config: MemoryConfig) -> bool:
        return True

    monkeypatch.setattr(runtime, "_probe_processing", probe_processing)
    async with runtime.module.lifecycle():
        assert await runtime._reconcile_locked(current) == {
            "ok": True,
            "state": "ready",
        }

    calls = 0

    async def ambiguous_add(_capture) -> None:
        nonlocal calls
        calls += 1
        raise MemoryProviderFailure("memory_provider_timeout", ambiguous=True)

    recovered = asyncio.Event()
    restart_once = runtime._restart_once

    async def observed_restart() -> dict[str, object]:
        try:
            return await restart_once()
        finally:
            recovered.set()

    monkeypatch.setattr(runtime, "_restart_once", observed_restart)
    runtime.module.replace_provider(FakeMemoryProvider(add_hook=ambiguous_add))
    principal = store.principal_for_user_key("slack:U123")

    assert await runtime.module.capture(
        CaptureRequest(
            source_message_id="source-ambiguous",
            session_id="session-1",
            principal_id=principal,
            project_id="default",
            provenance="user_input",
            text="remember this",
            occurred_at_ms=1_000,
        )
    ) == CaptureAccepted()
    await asyncio.wait_for(recovered.wait(), timeout=1.0)

    assert calls == 1
    assert len(process_factory.supervised) == 2
    assert process_factory.supervised[0].stopped
    assert process_factory.supervised[1].running
    assert not runtime.module._writer.unavailable
    reservation = runtime.module._writer.reserve("after-recovery")
    assert not isinstance(reservation, str)
    reservation.release()
    await runtime.close()


def test_runtime_store_is_identity_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with runtime._store._connection() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(row[0]).startswith("sqlite_")
        }
    assert tables == {"memory_meta", "memory_projects"}
