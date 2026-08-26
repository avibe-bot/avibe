"""Focused runtime lifecycle tests for best-effort Memory delivery."""

import asyncio
import concurrent.futures
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import core.memory.runtime as runtime_module
from config.v2_config import MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.everos import FakeMemoryProvider, ProviderHealthSnapshot
from core.memory.processing_record import RuntimeHealthProjection, SourceObservation
from core.memory.runtime import MemoryConfig, MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import (
    MemoryItem,
    MemoryItems,
    MemoryListItem,
    MemoryListPage,
    RecallItems,
    RecallPolicy,
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
async def test_aggregate_list_cursor_isolated_encoder_round_trips_large_id() -> None:
    provider_id = "episode-" + "x" * 10_000
    projects = ("default",)
    fingerprint = runtime_module._memory_list_catalog_fingerprint(
        "u-11111111111111111111111111111111",
        projects,
        origin="user",
    )

    cursor = await runtime_module._encode_memory_list_cursor_isolated(
        fingerprint,
        {"default": ("2026-08-26T00:00:00Z", provider_id)},
        {"default": 1},
        {"default": 2},
        timeout_seconds=5.0,
    )

    boundaries, page_hints, total_hints = runtime_module._decode_memory_list_cursor(
        cursor,
        projects=projects,
        fingerprint=fingerprint,
    )
    assert boundaries["default"] == ("2026-08-26T00:00:00Z", provider_id)
    assert page_hints == {"default": 1}
    assert total_hints == {"default": 2}


@pytest.mark.asyncio
async def test_cancelled_cursor_spool_unlinks_late_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    spool = tmp_path / "late-cursor.token"
    spool.write_text("cursor", encoding="ascii")

    def delayed_spool(_cursor: object, _deadline: float) -> str:
        entered.set()
        release.wait(timeout=2)
        return str(spool)

    monkeypatch.setattr(
        runtime_module,
        "_spool_memory_list_cursor",
        delayed_spool,
    )

    task = asyncio.create_task(
        runtime_module._decode_memory_list_cursor_isolated(
            "cursor",
            projects=("default",),
            fingerprint="fingerprint",
            timeout_seconds=5.0,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert spool.exists()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not spool.exists()


@pytest.mark.asyncio
async def test_aggregate_list_incrementally_retains_only_final_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    principal_id = "u-11111111111111111111111111111111"
    projects = tuple(f"project-{index:02d}" for index in range(17))
    monkeypatch.setattr(runtime, "list_memory_projects", lambda _principal: projects)

    async def decode_cursor(*_args, **_kwargs):
        return (
            {project: None for project in projects},
            {project: 1 for project in projects},
            {project: None for project in projects},
        )

    async def project_window(
        _principal_id: str,
        project_id: str,
        **_kwargs,
    ):
        project_index = projects.index(project_id)
        items = tuple(
            MemoryListItem(
                id=f"{project_id}-episode-{item_index:02d}",
                subject="subject",
                summary="summary",
                body="body",
                timestamp=(
                    f"2026-08-{project_index + 1:02d}T00:00:{item_index:02d}Z"
                ),
                project=project_id,
            )
            for item_index in range(20)
        )
        return (
            items,
            len(items),
            (),
            False,
            True,
            {item.id: 1 for item in items},
        )

    encoded: list[dict[str, tuple[str, str] | None]] = []

    async def encode_cursor(
        _fingerprint,
        boundaries,
        _page_hints,
        _total_hints,
        *,
        timeout_seconds,
    ):
        assert timeout_seconds > 0
        encoded.append(boundaries)
        return "isolated-cursor"

    merge_sizes: list[int] = []
    real_merge = runtime_module._merge_memory_list_candidates

    async def bounded_merge(items, *, limit: int, deadline: float, worker):
        assert deadline > time.monotonic()
        assert worker is not None
        buffered = list(items)
        merge_sizes.append(len(buffered))
        return real_merge(buffered, limit=limit)

    monkeypatch.setattr(
        runtime_module,
        "_decode_memory_list_cursor_isolated",
        decode_cursor,
    )
    monkeypatch.setattr(runtime, "_list_project_window", project_window)
    monkeypatch.setattr(
        runtime_module,
        "_encode_memory_list_cursor_isolated",
        encode_cursor,
    )
    monkeypatch.setattr(
        runtime_module,
        "_merge_memory_list_candidates_isolated",
        bounded_merge,
    )

    payload = await runtime.list_all_episodes_payload(
        principal_id,
        cursor=None,
        limit=20,
    )

    assert payload["status"] == "ok"
    assert payload["count"] == 20
    assert payload["next_cursor"] == "isolated-cursor"
    assert len(merge_sizes) == len(projects)
    assert max(merge_sizes) <= 40
    assert len(encoded) == 1
    await runtime.close()


def test_cursor_spool_lock_wait_honors_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    class BusyLock:
        def acquire(self, *, timeout: float) -> bool:
            timeouts.append(timeout)
            return False

        def release(self) -> None:
            raise AssertionError("an unacquired lock must not be released")

    monkeypatch.setattr(runtime_module, "_MEMORY_LIST_CURSOR_SPOOL_LOCK", BusyLock())
    with (tmp_path / "cursor").open("wb") as spool:
        with pytest.raises(asyncio.TimeoutError):
            runtime_module._write_memory_list_cursor_spool_chunk(
                spool,
                b"cursor",
                time.monotonic() + 1.0,
            )

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 1.0


@pytest.mark.asyncio
async def test_all_project_search_incrementally_retains_only_final_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    principal_id = "u-11111111111111111111111111111111"
    projects = tuple(f"project-{index:02d}" for index in range(17))
    monkeypatch.setattr(runtime, "list_memory_projects", lambda _principal: projects)

    async def resolve_mode(_policy: RecallPolicy) -> str:
        return "hybrid"

    async def recall(
        _query: str,
        *,
        policy: RecallPolicy,
        project_id: str,
        **_kwargs,
    ) -> RecallItems:
        assert policy.max_results == 20
        project_index = projects.index(project_id)
        return RecallItems(
            items=tuple(
                MemoryItem(
                    kind="episode",
                    text=f"{project_id}-item-{item_index:02d}",
                    date=f"2026-08-{project_index + 1:02d}",
                )
                for item_index in range(20)
            ),
            requested_mode=policy.mode,
            effective_mode="hybrid",
        )

    merge_sizes: list[int] = []
    real_merge = runtime_module._merge_search_items

    async def bounded_merge(items, *, limit: int, deadline: float, worker):
        assert deadline > time.monotonic()
        assert isinstance(worker, runtime_module._TerminableMemoryWorker)
        buffered = list(items)
        merge_sizes.append(len(buffered))
        return real_merge(buffered, limit=limit)

    monkeypatch.setattr(runtime.module, "resolve_recall_mode", resolve_mode)
    monkeypatch.setattr(runtime.module, "recall", recall)
    monkeypatch.setattr(
        runtime_module,
        "_merge_search_items_isolated",
        bounded_merge,
    )

    result = await runtime._recall_all_projects(
        "query",
        policy=RecallPolicy(
            mode="hybrid",
            max_results=20,
            include_profile=False,
        ),
        principal_id=principal_id,
    )

    assert isinstance(result, RecallItems)
    assert len(result.items) == 20
    assert len(merge_sizes) == len(projects)
    assert max(merge_sizes) <= 40
    await runtime.close()


@pytest.mark.asyncio
async def test_all_project_search_merge_round_trips_through_process_worker() -> None:
    worker = runtime_module._TerminableMemoryWorker()
    try:
        items = await runtime_module._merge_search_items_isolated(
            (
                MemoryItem(
                    kind="episode",
                    text="older",
                    date="2026-08-25",
                    project="default",
                ),
                MemoryItem(
                    kind="episode",
                    text="newer",
                    date="2026-08-26",
                    project="default",
                ),
            ),
            limit=1,
            deadline=time.monotonic() + 5,
            worker=worker,
        )
    finally:
        await worker.close()

    assert [item.text for item in items] == ["newer"]


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
