"""Focused runtime lifecycle tests for best-effort Memory delivery."""

import asyncio
import concurrent.futures
import sqlite3
from pathlib import Path

import pytest

import core.memory.runtime as runtime_module
from config.v2_config import MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.everos import FakeMemoryProvider, ProviderHealthSnapshot
from core.memory.processing_record import RuntimeHealthProjection, SourceObservation
from core.memory.runtime import MemoryConfig, MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import (
    MemoryItems,
    MemoryListItem,
    MemoryListPage,
    is_opaque_provider_id,
)


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
@pytest.mark.parametrize(
    ("warnings", "expected_warning"),
    [
        ((), "empty"),
        (("memory_search_partial",), None),
    ],
)
async def test_profile_payload_only_labels_confirmed_empty_results(
    tmp_path: Path,
    warnings: tuple[str, ...],
    expected_warning: str | None,
) -> None:
    runtime = _runtime(tmp_path)

    async def profile(**_kwargs: object) -> MemoryItems:
        return MemoryItems(warnings=warnings)

    runtime.module.profile = profile

    payload = await runtime.profile_payload("owner-1", "default")

    assert payload == {
        "status": "ok",
        "items": [],
        "warnings": list(warnings),
        "profile_warning": expected_warning,
    }
    await runtime.close()


def _write_legacy_clear_journal(store: MemoryStore, *, open_slot: object = None) -> Path:
    journal = store.path.with_name("clear-journal.sqlite")
    connection = sqlite3.connect(journal)
    connection.execute(
        "CREATE TABLE clear_operation (operation_id, operator_ref, target_epoch, "
        "state, resolution, open_slot)"
    )
    connection.execute(
        "INSERT INTO clear_operation VALUES (?, ?, ?, ?, ?, ?)",
        ("operation", "operator", 1, "completed", "done", open_slot),
    )
    connection.commit()
    connection.close()
    journal.chmod(0o600)
    return journal


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
async def test_terminal_released_clear_journal_is_discarded_without_repair(
    tmp_path: Path,
) -> None:
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    journal = _write_legacy_clear_journal(store)

    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )

    assert runtime.needs_repair is False
    assert not journal.exists()
    await runtime.close()


@pytest.mark.asyncio
async def test_open_released_clear_journal_remains_repair_fenced(
    tmp_path: Path,
) -> None:
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    journal = _write_legacy_clear_journal(store, open_slot=1)

    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )

    assert runtime.needs_repair is True
    assert runtime.needs_repair_reason == "memory_legacy_recovery_required"
    assert journal.exists()
    await runtime.close()


@pytest.mark.asyncio
async def test_malformed_released_clear_journal_remains_repair_fenced(
    tmp_path: Path,
) -> None:
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    journal = store.path.with_name("clear-journal.sqlite")
    journal.write_bytes(b"not sqlite")
    journal.chmod(0o600)

    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        store=store,
        effective_home=tmp_path,
    )

    assert runtime.needs_repair is True
    assert runtime.needs_repair_reason == "memory_legacy_recovery_required"
    assert journal.exists()
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


def test_aggregate_list_cursor_is_bound_to_selected_owner() -> None:
    """MEMORY-LIST-008: a user cursor cannot resume Agent browsing."""

    projects = ("default",)
    user_fingerprint = runtime_module._memory_list_catalog_fingerprint(
        "u-11111111111111111111111111111111",
        projects,
        origin="user",
    )
    cursor = runtime_module._encode_memory_list_cursor(
        user_fingerprint,
        {"default": None},
        {"default": 1},
        {"default": None},
    )
    agent_fingerprint = runtime_module._memory_list_catalog_fingerprint(
        "u-11111111111111111111111111111111",
        projects,
        origin="agent",
    )

    with pytest.raises(ValueError, match="invalid Memory list cursor"):
        runtime_module._decode_memory_list_cursor(
            cursor,
            projects=projects,
            fingerprint=agent_fingerprint,
        )


@pytest.mark.parametrize(
    "provider_id",
    (
        "episode-" + "x" * 10_000,
        "episode-\x00opaque",
        "episode-\ncontrol",
        "episode-\u8bb0",
    ),
)
def test_aggregate_list_cursor_round_trips_every_accepted_provider_id(
    provider_id: str,
) -> None:
    projects = ("default",)
    fingerprint = runtime_module._memory_list_catalog_fingerprint(
        "u-11111111111111111111111111111111",
        projects,
        origin="user",
    )
    assert is_opaque_provider_id(provider_id)
    cursor = runtime_module._encode_memory_list_cursor(
        fingerprint,
        {"default": ("2026-08-26T00:00:00Z", provider_id)},
        {"default": 1},
        {"default": 2},
    )

    boundaries, page_hints, total_hints = runtime_module._decode_memory_list_cursor(
        cursor,
        projects=projects,
        fingerprint=fingerprint,
    )

    if len(provider_id) > 8192:
        assert len(cursor.encode("ascii")) > 8192
    assert boundaries == {
        "default": ("2026-08-26T00:00:00Z", provider_id),
    }
    assert page_hints == {"default": 1}
    assert total_hints == {"default": 2}


@pytest.mark.asyncio
async def test_aggregate_list_pages_round_trip_opaque_nul_provider_id(
    tmp_path: Path,
) -> None:
    provider_id = "episode-\x00opaque"
    runtime = _runtime(tmp_path)
    runtime.module.replace_provider(
        FakeMemoryProvider(
            list_page=MemoryListPage(
                items=(
                    MemoryListItem(
                        id=provider_id,
                        subject="newer",
                        summary="newer",
                        body="newer body",
                        timestamp="2026-08-26T00:00:01Z",
                        project="default",
                    ),
                    MemoryListItem(
                        id="episode-older",
                        subject="older",
                        summary="older",
                        body="older body",
                        timestamp="2026-08-26T00:00:00Z",
                        project="default",
                    ),
                ),
                page=1,
                page_size=20,
                count=2,
                total_count=2,
            )
        )
    )

    first = await runtime.list_all_episodes_payload(
        "u-11111111111111111111111111111111",
        cursor=None,
        limit=1,
    )
    second = await runtime.list_all_episodes_payload(
        "u-11111111111111111111111111111111",
        cursor=first["next_cursor"],
        limit=1,
    )

    assert [item["id"] for item in first["items"]] == [provider_id]
    assert first["next_cursor"] is not None
    assert [item["id"] for item in second["items"]] == ["episode-older"]
    assert second["next_cursor"] is None
    await runtime.close()


@pytest.mark.asyncio
async def test_aggregate_cursor_timeout_terminates_isolated_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        def __init__(self) -> None:
            self.terminated = False
            self.joined = False

        def terminate(self) -> None:
            self.terminated = True

        def join(self) -> None:
            self.joined = True

    process = Process()

    class HangingExecutor:
        def __init__(self, *, max_workers: int, mp_context: object) -> None:
            assert max_workers == 1
            assert mp_context is context
            self._processes = {1: process}
            self.shutdown_called = False

        def submit(self, _operation, *_args):
            return concurrent.futures.Future()

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            assert wait is True
            assert cancel_futures is True
            self.shutdown_called = True

    context = object()
    executors: list[HangingExecutor] = []

    def executor_factory(**kwargs):
        executor = HangingExecutor(**kwargs)
        executors.append(executor)
        return executor

    monkeypatch.setattr(runtime_module.multiprocessing, "get_context", lambda method: context)
    monkeypatch.setattr(
        runtime_module.concurrent.futures,
        "ProcessPoolExecutor",
        executor_factory,
    )

    with pytest.raises(asyncio.TimeoutError):
        await runtime_module._decode_memory_list_cursor_isolated(
            "e30",
            projects=("default",),
            fingerprint="fingerprint",
            timeout_seconds=0.01,
        )

    assert executors[0].shutdown_called
    assert process.terminated
    assert process.joined
