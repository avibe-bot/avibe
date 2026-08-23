"""Focused runtime lifecycle tests for volatile Memory delivery."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from config.v2_config import MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.everos import FakeMemoryProvider
from core.memory.process import FakeEverOSProcessFactory
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
async def test_missing_provider_call_log_remains_unavailable(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    projection = await runtime._processing_record_provider_checks(None)

    assert projection.source.status == "unavailable"
    assert projection.source.reason == "provider_call_log_unavailable"
    assert projection.items == ()
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


def test_runtime_store_is_identity_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    with runtime._store._connection() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(row[0]).startswith("sqlite_")
        }
    assert tables == {"memory_meta", "memory_projects"}
