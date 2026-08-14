from __future__ import annotations

import asyncio
from collections import deque

import pytest

from core.memory.everos import (
    AddAck,
    FakeMemoryProvider,
    FlushSucceeded,
    MemoryProviderPort,
    ProviderCapture,
)
from core.memory.types import MemoryListItem, MemoryListPage, ProviderSessionRef


SESSION_REF = ProviderSessionRef(
    principal_id="u-11111111111111111111111111111111",
    epoch=1,
    project_ref="p-22222222222222222222222222222222",
    session_id="provider-session",
)
CAPTURE = ProviderCapture(
    session_ref=SESSION_REF,
    text="remember this",
    provider_timestamp_ms=1_725_000_001_234,
)


async def test_add_hook_runs_after_failure_and_recording_but_before_results() -> None:
    failure = RuntimeError("configured add failure")
    configured = AddAck(request_id="configured-add", status="accumulated")
    observations: list[tuple[int, int, int]] = []

    async def observe(_capture: ProviderCapture) -> None:
        observations.append(
            (
                len(provider.captures),
                len(provider.ingest_failures),
                len(provider.add_results),
            )
        )

    provider = FakeMemoryProvider(
        ingest_failures=deque((failure,)),
        add_results=deque((configured,)),
        add_hook=observe,
    )

    with pytest.raises(RuntimeError) as raised:
        await provider.add(CAPTURE)
    assert raised.value is failure
    assert provider.captures == []

    assert await provider.add(CAPTURE) is configured
    default = await provider.add(CAPTURE)

    assert default == AddAck(request_id="fake-add-2", status="accumulated")
    assert provider.captures == [CAPTURE, CAPTURE]
    assert observations == [(1, 0, 1), (2, 0, 0)]


async def test_flush_hook_runs_after_recording_but_before_existing_results() -> None:
    configured = FlushSucceeded(request_id="configured-flush", status="extracted")
    observations: list[tuple[int, int]] = []

    async def observe(_session_ref: ProviderSessionRef) -> None:
        observations.append((len(provider.flushes), len(provider.flush_results)))

    provider = FakeMemoryProvider(
        flush_results=deque((configured,)),
        flush_hook=observe,
    )

    assert await provider.flush(SESSION_REF) is configured
    default = await provider.flush(SESSION_REF)

    assert default == FlushSucceeded(
        request_id="fake-flush-2",
        status="extracted",
    )
    assert provider.flushes == [SESSION_REF, SESSION_REF]
    assert observations == [(1, 1), (2, 0)]


async def test_processing_healthy_hook_runs_before_failure_and_default() -> None:
    failure = RuntimeError("configured processing health failure")
    observed_failures: list[BaseException | None] = []

    async def observe() -> None:
        observed_failures.append(provider.processing_health_failure)

    provider = FakeMemoryProvider(
        processing_healthy_flag=False,
        processing_health_failure=failure,
        processing_healthy_hook=observe,
    )

    with pytest.raises(RuntimeError) as raised:
        await provider.processing_healthy()
    assert raised.value is failure

    provider.processing_health_failure = None
    assert await provider.processing_healthy() is False
    assert observed_failures == [failure, None]


async def test_hook_exception_propagates_after_recording_before_add_result() -> None:
    hook_failure = RuntimeError("add hook failed")
    configured_result = AddAck(request_id="configured-add", status="accumulated")

    async def fail(_capture: ProviderCapture) -> None:
        raise hook_failure

    provider = FakeMemoryProvider(
        add_results=deque((configured_result,)),
        add_hook=fail,
    )

    with pytest.raises(RuntimeError) as raised:
        await provider.add(CAPTURE)

    assert raised.value is hook_failure
    assert provider.captures == [CAPTURE]
    assert tuple(provider.add_results) == (configured_result,)


async def test_hook_cancellation_propagates_after_flush_recording() -> None:
    configured = FlushSucceeded(request_id="configured-flush", status="extracted")

    async def cancel(_session_ref: ProviderSessionRef) -> None:
        raise asyncio.CancelledError

    provider = FakeMemoryProvider(
        flush_results=deque((configured,)),
        flush_hook=cancel,
    )

    with pytest.raises(asyncio.CancelledError):
        await provider.flush(SESSION_REF)

    assert provider.flushes == [SESSION_REF]
    assert tuple(provider.flush_results) == (configured,)


async def test_absent_hooks_preserve_default_fake_behavior() -> None:
    add_failure = RuntimeError("configured add failure")
    processing_failure = RuntimeError("configured processing health failure")
    configured_add = AddAck(request_id="configured-add", status="accumulated")
    configured_flush = FlushSucceeded(
        request_id="configured-flush",
        status="extracted",
    )
    provider = FakeMemoryProvider(
        processing_healthy_flag=False,
        ingest_failures=deque((add_failure,)),
        add_results=deque((configured_add,)),
        flush_results=deque((configured_flush,)),
        processing_health_failure=processing_failure,
    )

    with pytest.raises(RuntimeError) as raised:
        await provider.add(CAPTURE)
    assert raised.value is add_failure
    assert provider.captures == []
    assert await provider.add(CAPTURE) is configured_add
    assert await provider.add(CAPTURE) == AddAck(
        request_id="fake-add-2",
        status="accumulated",
    )

    assert await provider.flush(SESSION_REF) is configured_flush
    assert await provider.flush(SESSION_REF) == FlushSucceeded(
        request_id="fake-flush-2",
        status="extracted",
    )

    with pytest.raises(RuntimeError) as raised:
        await provider.processing_healthy()
    assert raised.value is processing_failure
    provider.processing_health_failure = None
    assert await provider.processing_healthy() is False


def test_timing_hooks_are_fake_only() -> None:
    provider = FakeMemoryProvider()

    assert isinstance(provider, MemoryProviderPort)
    assert not hasattr(MemoryProviderPort, "add_hook")
    assert not hasattr(MemoryProviderPort, "flush_hook")
    assert not hasattr(MemoryProviderPort, "processing_healthy_hook")


async def test_list_fake_records_exact_page_request_and_returns_configured_items() -> None:
    item = MemoryListItem(
        id="opaque-id",
        subject="Subject",
        summary="Summary",
        body="Body",
        timestamp="2026-08-14T00:00:00Z",
        project="notes",
    )
    provider = FakeMemoryProvider(
        list_page=MemoryListPage(
            items=(item,),
            page=1,
            page_size=20,
            count=1,
            total_count=1,
        )
    )

    result = await provider.list_episodes(
        SESSION_REF.principal_id,
        "notes",
        3,
        5,
    )

    assert result == MemoryListPage(
        items=(item,),
        page=3,
        page_size=5,
        count=1,
        total_count=1,
    )
    assert provider.list_requests == [(SESSION_REF.principal_id, "notes", 3, 5)]
