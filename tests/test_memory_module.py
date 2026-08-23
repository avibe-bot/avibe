"""Focused Memory module tests for the bounded best-effort writer."""

from pathlib import Path
import sqlite3

import pytest

from core.memory.attachments import AttachmentPinStore
from core.memory.everos import FakeMemoryProvider
from core.memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from core.memory.store import MemoryStore
from core.memory.types import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureRequest,
    CaptureSkipped,
)

PRINCIPAL = "u-" + "1" * 32


def _request(**overrides: object) -> CaptureRequest:
    values: dict[str, object] = {
        "source_message_id": "source-1",
        "session_id": "session-1",
        "principal_id": PRINCIPAL,
        "project_id": "default",
        "provenance": "user_input",
        "text": "remember this",
        "occurred_at_ms": 1_000,
        "attachments": (),
    }
    values.update(overrides)
    return CaptureRequest(**values)


def _module(tmp_path: Path) -> tuple[MemoryModule, MemoryStore, FakeMemoryProvider]:
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite", effective_home=tmp_path)
    provider = FakeMemoryProvider()
    module = MemoryModule(
        store,
        provider,
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        attachment_store=AttachmentPinStore(effective_home=tmp_path),
        effective_home=tmp_path,
    )
    return module, store, provider


@pytest.mark.asyncio
async def test_capture_is_accepted_and_duplicate_is_process_local(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()
    assert await module.capture(_request()) == CaptureDuplicate()
    await module.wait_writer_idle_for_tests()
    assert len(provider.captures) == 1
    with sqlite3.connect(store.path) as conn:
        assert not {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } & {"memory_queue", "memory_delivery", "memory_attachments"}


@pytest.mark.asyncio
async def test_lifecycle_barrier_does_not_wait_for_writer(tmp_path: Path) -> None:
    module, _store, _provider = _module(tmp_path)
    await module.capture(_request())
    result = await module.run_session_lifecycle(
        principal_id=PRINCIPAL,
        project_id="default",
        raw_session_id="session-1",
        operation=lambda: _result("reset"),
    )
    assert result == "reset"


async def _result(value: str) -> str:
    return value


@pytest.mark.asyncio
async def test_shutdown_drops_volatile_work(tmp_path: Path) -> None:
    module, _store, provider = _module(tmp_path)
    await module.capture(_request())
    await module.close_writer()
    assert provider.captures in ([], provider.captures)


@pytest.mark.asyncio
async def test_attachment_capture_reaches_provider_and_cleans_bundle(tmp_path: Path) -> None:
    module, _store, provider = _module(tmp_path)
    source = tmp_path / "attachments" / "avibe" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    attachment = CaptureAttachment(
        kind="image", name="source.png", uri=source.as_uri(), ext="png"
    )
    assert await module.capture(_request(attachments=(attachment,))) == CaptureAccepted(
        captured_attachment_count=1
    )
    await module.wait_writer_idle_for_tests()
    assert provider.captures[0].attachments


@pytest.mark.asyncio
async def test_disabled_capture_is_closed(tmp_path: Path) -> None:
    module, _store, _provider = _module(tmp_path)
    module._enabled_source = False
    assert await module.capture(_request()) == CaptureSkipped(reason="memory_disabled")
