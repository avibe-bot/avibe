from __future__ import annotations

import asyncio
from pathlib import Path

from config.v2_config import MemoryConfig
from core.memory.runtime import MemoryRuntime


PRINCIPAL = "u-11111111111111111111111111111111"
PROJECT = "p-22222222222222222222222222222222"


def _enqueue(runtime: MemoryRuntime, source: str) -> None:
    result = runtime._store.enqueue_request(
        source_message_id=source,
        session_id="session",
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        provenance="user_input",
        payload_text="private payload",
        occurred_at_ms=1,
        max_provider_timestamp_ms=100,
    )
    assert result.outcome == "accepted"


async def test_interrupted_queue_delete_requires_explicit_resume_at_target_epoch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "resume-source")
    pre_epoch = runtime._store.ensure_meta().epoch
    journal = runtime._clear_journal
    assert journal is not None
    original_record = journal.record_surface_deleted
    interrupted = False

    def interrupt_queue_receipt(operation_id, surface, **kwargs):
        nonlocal interrupted
        if surface == "queue" and not interrupted:
            interrupted = True
            raise OSError("injected receipt failure")
        return original_record(operation_id, surface, **kwargs)

    monkeypatch.setattr(journal, "record_surface_deleted", interrupt_queue_receipt)

    failed = await runtime.clear(operator_ref="user:owner")

    assert failed["status"] == "failed"
    recovery = journal.get_open_operation()
    assert recovery is not None
    assert recovery.state == "recovery_needed"
    assert recovery.recovery_from_state == "deleting"
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert runtime._store.list_queue_rows() == ()

    monkeypatch.setattr(journal, "record_surface_deleted", original_record)
    completed = await runtime.resume_clear(
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert completed["status"] == "completed"
    assert completed["epoch"] == pre_epoch + 1
    assert runtime._store.ensure_meta().epoch == pre_epoch + 1
    assert journal.get_open_operation() is None
    await runtime.close()


async def test_abort_restores_all_surfaces_after_destructive_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime = MemoryRuntime(MemoryConfig(), effective_home=tmp_path)
    _enqueue(runtime, "abort-source")
    pre_meta = runtime._store.ensure_meta()
    original_delete = runtime._delete_clear_surface

    async def interrupt_provider(surface, *, target_epoch):
        if surface.surface == "provider":
            raise OSError("injected provider delete failure")
        await original_delete(surface, target_epoch=target_epoch)

    monkeypatch.setattr(runtime, "_delete_clear_surface", interrupt_provider)
    failed = await runtime.clear(operator_ref="user:owner")
    recovery = runtime._clear_journal.get_open_operation()

    assert failed["status"] == "failed"
    assert recovery is not None and recovery.destructive_started
    assert runtime._store.list_queue_rows() == ()

    monkeypatch.setattr(runtime, "_delete_clear_surface", original_delete)
    aborted = await runtime.abort_clear(
        recovery.operation_id,
        operator_ref="user:owner",
    )

    assert aborted["status"] == "aborted"
    restored_meta = runtime._store.ensure_meta()
    assert restored_meta.epoch == pre_meta.epoch
    assert restored_meta.clear_in_progress is False
    assert len(runtime._store.list_queue_rows()) == 1
    assert runtime._clear_journal.get_open_operation() is None
    await runtime.close()
