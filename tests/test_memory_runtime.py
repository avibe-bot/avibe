"""Focused runtime lifecycle tests for best-effort Memory delivery."""

import asyncio
from pathlib import Path

import pytest

import core.memory.runtime as runtime_module
from config.v2_config import MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.everos import ProviderHealthSnapshot
from core.memory.processing_record import RuntimeHealthProjection, SourceObservation
from core.memory.runtime import MemoryConfig, MemoryRuntime
from core.memory.store import MemoryStore


def _runtime(tmp_path: Path) -> MemoryRuntime:
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
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

    assert runtime.offer_barrier("session") == "queued"
    assert offered == ["session"]
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_drops_volatile_work(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    await runtime.close()
    assert runtime.closed


@pytest.mark.asyncio
async def test_environmental_store_failure_defers_durable_repair_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_type = runtime_module.MemoryStore

    def unavailable_store(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(runtime_module, "MemoryStore", unavailable_store)
    config = MemoryConfig(enabled=True, legacy_needs_repair=True)
    runtime = MemoryRuntime(config, effective_home=tmp_path)

    runtime.mark_needs_repair("memory_local_data_unusable")

    assert runtime.available is False
    assert runtime.needs_repair is False
    assert runtime.needs_repair_reason == "memory_local_data_unusable"
    assert runtime.runtime_state() == "degraded"
    status = await runtime.status_payload()
    assert status["state"] == "degraded"
    assert status["reason"] == "memory_permission_denied"

    monkeypatch.setattr(runtime_module, "MemoryStore", store_type)
    result = await runtime.reconcile(config)

    assert result["state"] == "needs_repair"
    assert runtime.needs_repair is True
    await runtime.close()


@pytest.mark.asyncio
async def test_disabled_legacy_recovery_remains_visible_as_needs_repair(
    tmp_path: Path,
) -> None:
    runtime = MemoryRuntime(
        MemoryConfig(enabled=False, legacy_needs_repair=True),
        effective_home=tmp_path,
    )

    assert runtime.runtime_state() == "needs_repair"
    await runtime.close()


@pytest.mark.asyncio
async def test_disabling_repair_fenced_runtime_stops_without_clearing_fence(
    tmp_path: Path,
) -> None:
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True, legacy_needs_repair=True),
        effective_home=tmp_path,
    )

    result = await runtime.reconcile(
        MemoryConfig(enabled=False, legacy_needs_repair=True),
    )

    assert result == {
        "ok": True,
        "state": "needs_repair",
        "error": "memory_legacy_recovery_required",
    }
    assert runtime._config.enabled is False
    assert runtime.runtime_state() == "needs_repair"
    assert runtime.needs_repair_reason == "memory_legacy_recovery_required"
    assert runtime._supervisor.status.running is False
    await runtime.close()


@pytest.mark.asyncio
async def test_unreadable_released_clear_state_fails_closed_as_needs_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )

    def unreadable_clear_state() -> bool:
        raise OSError("database is locked")

    monkeypatch.setattr(store, "clear_in_progress", unreadable_clear_state)
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )

    assert runtime.needs_repair is True
    assert runtime.needs_repair_reason == "memory_legacy_recovery_required"
    assert (await runtime.status_payload())["state"] == "needs_repair"
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_cancels_pending_automatic_wake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-INDEP-003: shutdown cancels volatile recovery work."""

    runtime = _runtime(tmp_path)
    wake_entered = asyncio.Event()

    async def pending_wake() -> None:
        wake_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_wake_after_writer_close", pending_wake)
    runtime._defer_wake_until_writer_closed()
    wake = runtime._wake_task
    assert wake is not None
    await wake_entered.wait()

    await asyncio.wait_for(runtime.close(), timeout=1.0)

    assert wake.cancelled()
    assert runtime._wake_task is None
    assert runtime.closed


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
async def test_status_uses_unified_state_and_omits_cascade_protocol(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    async def healthy_status() -> RuntimeHealthProjection:
        return RuntimeHealthProjection(
            SourceObservation("available"),
            ProviderHealthSnapshot(
                status="ok",
                version="test",
                capabilities={"embed": True},
                disabled_features=(),
                cascade={"pending": 9},
            ),
        )

    runtime._processing_record.read_status = healthy_status

    payload = await runtime.status_payload()

    assert payload["state"] in {"starting", "running", "degraded"}
    assert "reason" in payload
    assert "cascade" not in payload["health"]
    await runtime.close()


@pytest.mark.asyncio
async def test_disabled_attachment_intake_is_unavailable_in_status(
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

    assert (await runtime.status_payload())["attachment_capture"] == {
        "status": "unavailable"
    }
    assert runtime.attachment_capture_config_generation() is None
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
