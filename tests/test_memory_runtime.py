"""Focused runtime lifecycle tests for best-effort Memory delivery."""

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import avibe_memory.runtime as runtime_module
from avibe_memory.capture_adapter import EnabledMemoryAdapter
from config.v2_config import (
    MemoryCloudCapabilities,
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
)
from avibe_memory.everos import (
    MemoryPreflightDiagnostic,
    MemoryPreflightFailure,
    ProviderHealthSnapshot,
)
from avibe_memory.processing_record import (
    MaintenanceObservation,
    RuntimeHealthProjection,
    SourceObservation,
)
from avibe_memory.runtime import MemoryRuntime
from avibe_memory.store import MemoryStore
from avibe_memory.types import MemoryItems, MemoryListItem, MemoryListPage
from vibe.memory_contract import MemoryRuntimeBusyError


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


def test_typed_rerank_change_reaches_sidecar_without_embedding_rebuild() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(
                chat=True,
                embedding=True,
                memory_llm=True,
            ),
            memory_llm_source="chat_fallback",
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            revision=1,
            model_access_key="mak_opaque",
            rerank_access_key="mak_rr_deepinfra_opaque",
            access_key_revision=1,
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )
    candidate = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=current.cloud.capabilities,
            memory_llm_source="chat_fallback",
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            revision=1,
            model_access_key="mak_opaque",
            rerank_access_key="mak_rr_dashscope_opaque",
            access_key_revision=1,
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    settings = runtime_module._process_settings(candidate)  # noqa: SLF001

    assert current.runtime_processing() != candidate.runtime_processing()
    assert current.runtime_embedding_identity() == candidate.runtime_embedding_identity()
    assert runtime_module._embedding_configuration_changed(  # noqa: SLF001
        current,
        candidate,
    ) is False
    assert settings.rerank_base_url == (
        "https://backend.example.test/v1/model/rerank/dashscope"
    )
    assert settings.rerank_model == "gte-rerank-v2"
    assert settings.rerank_api_key == "mak_rr_dashscope_opaque"
    assert settings.rerank_provider == "dashscope"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("managed", "expected_sides", "expected_ok"),
    [
        (False, ["llm", "embedding", "rerank"], False),
        (True, ["llm", "embedding"], True),
    ],
)
async def test_candidate_preflight_validates_only_custom_rerank(
    managed: bool,
    expected_sides: list[str],
    expected_ok: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    custom_processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig(
            "https://llm.example.test/v1",
            "chat",
            "llm-secret",
        ),
        embedding=MemoryEndpointConfig(
            "https://embedding.example.test/v1",
            "embedding",
            "embedding-secret",
        ),
        rerank=MemoryEndpointConfig(
            "https://rerank.example.test/v1/inference",
            "rerank",
            "rerank-secret",
            provider="deepinfra",
        ),
    )
    if managed:
        candidate = MemoryConfig(
            enabled=True,
            mode="platform",
            cloud=MemoryCloudConfig(
                scope="platform",
                capabilities=MemoryCloudCapabilities(
                    chat=True,
                    embedding=True,
                    memory_llm=True,
                ),
                memory_llm_source="chat_fallback",
                embedding_identity="emb-v1",
                applied_embedding_identity="emb-v1",
                revision=2,
                model_access_key="mak_opaque",
                rerank_access_key="mak_rr_deepinfra_opaque",
                access_key_revision=2,
                proxy_base_url="https://backend.example.test/v1/model",
                source_instance_id="instance-1",
            ),
        )
    else:
        candidate = MemoryConfig(
            enabled=True,
            mode="custom",
            processing=custom_processing,
        )

    sides: list[str] = []

    async def probe(_provider, side, *_args):
        sides.append(side)
        if side == "rerank":
            return MemoryPreflightFailure(
                "memory_rerank_unavailable",
                MemoryPreflightDiagnostic(side, message="invalid_candidate"),
            )
        return None

    monkeypatch.setattr(runtime_module.EverOSPort, "_preflight_endpoint", probe)

    try:
        result = await runtime.preflight(candidate)
    finally:
        await runtime.close()

    assert sides == expected_sides
    assert result["ok"] is expected_ok
    if not expected_ok:
        assert result["error"] == "memory_rerank_unavailable"


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


@pytest.mark.asyncio
async def test_all_project_list_accepts_everos_maximum_page_size(
    tmp_path: Path,
) -> None:
    """MEMORY-LIST-009: the aggregate route accepts EverOS's 100-item maximum."""

    runtime = _runtime(tmp_path)
    calls: list[tuple[int, int]] = []
    items = tuple(
        MemoryListItem(
            id=f"episode-{index:03d}",
            subject=f"subject-{index}",
            summary=f"summary-{index}",
            body=f"body-{index}",
            timestamp=(
                datetime(2026, 8, 27, tzinfo=timezone.utc) - timedelta(minutes=index)
            ).isoformat(),
            project="default",
        )
        for index in range(100)
    )

    async def list_memory_projects(_principal_id: str) -> tuple[str, ...]:
        return ("default",)

    runtime.list_memory_projects = list_memory_projects

    async def list_episodes(**kwargs: object) -> MemoryListPage:
        page = int(kwargs["page"])
        page_size = int(kwargs["page_size"])
        calls.append((page, page_size))
        start = (page - 1) * page_size
        page_items = items[start : start + page_size]
        return MemoryListPage(
            items=page_items,
            page=page,
            page_size=page_size,
            count=len(page_items),
            total_count=len(items),
        )

    runtime.module.list_episodes = list_episodes
    runtime.module.concurrent_episode_lists = None
    principal_id = "u-11111111111111111111111111111111"

    payload = await runtime.list_all_episodes_payload(
        principal_id,
        cursor=None,
        limit=100,
    )

    assert payload["status"] == "ok"
    assert payload["count"] == 100
    assert {page for page, _page_size in calls} == set(range(1, 6))
    assert all(page_size == 20 for _page, page_size in calls)
    assert await runtime.list_all_episodes_payload(
        principal_id,
        cursor=None,
        limit=101,
    ) == {"status": "failed", "error": "memory_invalid_input"}
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
@pytest.mark.parametrize(
    "read_kind",
    ("sources", "maintenance", "principal", "projects"),
)
async def test_local_reads_hold_reset_and_fail_closed_after_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_kind: str,
) -> None:
    def fail_supervisor(**_kwargs):
        raise RuntimeError("supervisor construction failed")

    with pytest.raises(RuntimeError) as retained:
        MemoryRuntime(
            MemoryConfig(enabled=True),
            effective_home=tmp_path,
            supervisor_factory=fail_supervisor,
        )
    assert retained.traceback is not None
    runtime = _runtime(tmp_path)

    observation = MaintenanceObservation(block_reason=None, can_delete_data=True)
    principal_id = "u-11111111111111111111111111111111"
    read_call = {
        "sources": lambda: runtime._processing_record_sources(None),
        "maintenance": lambda: runtime._processing_record_maintenance(
            None, observation
        ),
        "principal": lambda: runtime.resolve_principal_for_user_key("avibe:local"),
        "projects": lambda: runtime.list_memory_projects(principal_id),
    }[read_kind]
    wake = None
    if read_kind == "sources":
        failure = await runtime._processing_record_failure_log(None)
        assert failure.items == ()
        assert failure.unavailable_reason == "memory_failure_history_unavailable"
        wake_entered = asyncio.Event()

        async def pending_wake() -> None:
            wake_entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime, "_wake_after_writer_close", pending_wake)
        runtime._defer_wake_until_writer_closed()
        wake = runtime._wake_task
        assert wake is not None
        await wake_entered.wait()
    run_local = runtime._run_local_observation
    started = threading.Event()
    release = threading.Event()

    async def blocked(operation):
        def run():
            started.set()
            assert release.wait(timeout=5.0)
            return operation()

        return await run_local(run)

    monkeypatch.setattr(runtime, "_run_local_observation", blocked)
    read = asyncio.create_task(read_call())

    try:
        assert await asyncio.to_thread(started.wait, 1.0)
        ownership = runtime.begin_root_ownership_handoff()

        async def close_and_reset():
            await runtime.close(root_ownership=ownership)
            return runtime.reset_mutable_data(ownership)

        resetting = asyncio.create_task(close_and_reset())
        while "local cleanup" not in runtime._close_phase_tasks:
            await asyncio.sleep(0)
        assert resetting.done() is False
        assert (tmp_path / "memory").exists()
    finally:
        release.set()
    await read
    assert (await resetting).data_deleted is True
    if wake is not None:
        assert wake.cancelled() and runtime._wake_task is None
    monkeypatch.setattr(runtime, "_run_local_observation", run_local)

    if read_kind in {"sources", "maintenance"}:
        detached = await read_call()
    else:
        with pytest.raises(MemoryRuntimeBusyError):
            await read_call()
    if read_kind == "sources":
        assert {
            detached.memcells.reason,
            detached.runs.reason,
            detached.semantic.reason,
        } == {"busy"}
        assert not hasattr(runtime, "log_entries_payload")
        assert not hasattr(runtime, "_call_log_db_path")
        failure = await runtime._processing_record_failure_log(None)
        assert failure.unavailable_reason == "busy"
    elif read_kind == "maintenance":
        assert detached.data_exists is True
        assert detached.can_delete_data is False
        assert detached.error == "busy"
    with pytest.raises(MemoryRuntimeBusyError):
        _runtime(tmp_path)

    if read_kind == "sources":
        replacement = runtime.replacement(MemoryConfig(enabled=True), ownership)
        with pytest.raises(MemoryRuntimeBusyError):
            _runtime(tmp_path)
        replacement.accept_root_ownership()
        await replacement.close()
    else:
        runtime.release_retained_root_ownership()
    await _runtime(tmp_path).close()


@pytest.mark.asyncio
async def test_runtime_close_timeouts_still_attempt_everos_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    release = asyncio.Event()
    phases: list[str] = []

    def stalled(label: str):
        async def wait() -> None:
            phases.append(label)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        return wait

    async def stop_provider() -> None:
        phases.append("provider")

    monkeypatch.setattr(runtime, "_cancel_capture_tasks", stalled("capture"))
    monkeypatch.setattr(runtime, "_close_local_runtime", stalled("local"))
    monkeypatch.setattr(runtime._supervisor, "close", stop_provider)

    with pytest.raises(RuntimeError, match="local cleanup"):
        await runtime.close(timeout_seconds=0.03)

    assert phases == ["capture", "local", "provider"]
    with pytest.raises(MemoryRuntimeBusyError):
        _runtime(tmp_path)
    release.set()
    await asyncio.sleep(0)
    await runtime.close(timeout_seconds=1.0)

    replacement = _runtime(tmp_path)
    await replacement.close()


@pytest.mark.asyncio
async def test_blocked_artifact_install_keeps_root_across_cached_close_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    install_started = threading.Event()
    install_release = threading.Event()

    def blocked_ensure(*, force: bool) -> dict[str, object]:
        assert force is True
        install_started.set()
        assert install_release.wait(timeout=2.0)
        return {"ok": True}

    monkeypatch.setattr(runtime._artifact_manager, "ensure", blocked_ensure)
    install = asyncio.create_task(runtime.install_artifact())
    assert await asyncio.to_thread(install_started.wait, 1.0)

    close = asyncio.create_task(runtime.close(timeout_seconds=1.0))
    while "local cleanup" not in runtime._close_phase_tasks:
        await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await runtime.close(timeout_seconds=0.03)
    assert close.done() is False
    with pytest.raises(MemoryRuntimeBusyError):
        _runtime(tmp_path)

    install_release.set()
    assert await install == {
        "ok": False,
        "reason": "memory_operation_in_progress",
        "download_error": None,
    }
    await close
    replacement = _runtime(tmp_path)
    await replacement.close()


@pytest.mark.parametrize("operation", ("reconcile", "wake"))
@pytest.mark.asyncio
async def test_revoked_lifecycle_stops_before_provider_root_side_effects(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()

    async def truthy(*_args) -> bool:
        return True

    async def delayed_stop() -> None:
        stop_started.set()
        await stop_release.wait()

    async def unexpected_async(name: str, *_args, **_kwargs):
        pytest.fail(f"revoked reconciliation reached {name}")

    def unexpected_sync(name: str, *_args, **_kwargs):
        pytest.fail(f"revoked reconciliation reached {name}")

    async def no_op() -> None:
        return None

    monkeypatch.setattr(runtime, "_reconcile_released_ownership", truthy)
    monkeypatch.setattr(runtime, "_probe_processing", truthy)
    monkeypatch.setattr(runtime, "artifact_admitted", lambda: True)
    monkeypatch.setattr(runtime._artifact_manager, "resolve_python", lambda: Path("python"))
    monkeypatch.setattr(runtime._supervisor, "stop", delayed_stop)
    monkeypatch.setattr(
        runtime._supervisor,
        "wake",
        lambda *_args, **_kwargs: unexpected_async("provider wake"),
    )
    monkeypatch.setattr(
        runtime._store,
        "ensure_meta",
        lambda: unexpected_sync("store metadata"),
    )
    monkeypatch.setattr(
        runtime._provider_root_owner,
        "ensure",
        lambda *_args: unexpected_sync("provider root ensure"),
    )
    monkeypatch.setattr(runtime, "_close_local_runtime", no_op)
    monkeypatch.setattr(runtime._supervisor, "close", no_op)

    lifecycle = asyncio.create_task(
        runtime.reconcile(MemoryConfig(enabled=True))
        if operation == "reconcile"
        else runtime.wake(operation_lease_held=True)
    )
    await stop_started.wait()
    runtime.begin_close()
    stop_release.set()

    expected = {"ok": False, "error": "memory_operation_in_progress"}
    if operation == "wake":
        expected["state"] = "starting"
    assert await lifecycle == expected
    await runtime.close()


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
async def test_store_recovery_keeps_facade_identity_and_starts_bound_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_type = runtime_module.MemoryStore

    def unavailable_store(*_args, **_kwargs):
        raise PermissionError("denied")

    async def acquire(_session_id: str) -> object:
        return type("Admission", (), {"release": lambda self: None})()

    monkeypatch.setattr(runtime_module, "MemoryStore", unavailable_store)
    runtime = MemoryRuntime(
        MemoryConfig(enabled=True),
        effective_home=tmp_path,
        is_enabled_user=lambda _platform, _user_id: True,
        lifecycle_snapshot_matches=lambda _session_id, _snapshot: True,
        acquire_lifecycle_admission=acquire,
    )
    facade = runtime.capture_adapter
    assert isinstance(facade, EnabledMemoryAdapter)
    assert runtime.available is False
    assert runtime.start_capture_adapter(
        task_factory=asyncio.get_running_loop().create_task
    ) is False

    monkeypatch.setattr(runtime_module, "MemoryStore", store_type)
    assert runtime._open_store() is True

    assert runtime.capture_adapter is facade
    assert runtime.available is True
    assert facade._worker_task is not None
    assert not facade._worker_task.done()
    with pytest.raises(RuntimeError, match="scheduler is already bound"):
        runtime.start_capture_adapter(task_factory=lambda _pending, **_kwargs: None)
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
    first_epoch = runtime.attachment_capture_config_generation()
    assert isinstance(first_epoch, int)
    runtime._replace_provider(runtime._provider)
    second_epoch = runtime.attachment_capture_config_generation()
    assert isinstance(second_epoch, int)
    assert second_epoch > first_epoch

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
