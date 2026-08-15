from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from config import paths
from core.memory.attachments import AttachmentPinError, AttachmentPinStore, PinnedBundle
from core.memory.everos import FakeMemoryProvider, MemoryProviderFailure
from core.memory.module import (
    MAX_CAPTURE_ATTACHMENT_METADATA_BYTES,
    MAX_CAPTURE_IDENTIFIER_BYTES,
    MAX_CAPTURE_TEXT_BYTES,
    MAX_QUERY_BYTES,
    MIN_FREE_DISK_BYTES,
    MemoryModule,
)
from core.memory.provider_root import ProviderRoot, ProviderRootMetadata
from core.memory.store import MemoryStore
from core.memory.types import (
    CLOSED_MEMORY_ERROR_CODES,
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureSkipped,
    MemoryItem,
    MemoryItems,
    MemoryListItem,
    MemoryListPage,
    MemoryListResult,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    OperationFailed,
    RecallItems,
    RecallPolicy,
)


PROJECT = "default"
PRINCIPAL = "u-11111111111111111111111111111111"


@pytest.fixture(autouse=True)
def isolated_avibe_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every store and attachment path inside this test's temporary tree."""

    home = tmp_path / "avibe-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("AVIBE_HOME", str(home))


def _store_path(scope: Path) -> Path:
    return paths.get_state_dir() / "memory-tests" / scope.name / "memory.sqlite"


def _attachment_store() -> AttachmentPinStore:
    home = paths.get_vibe_remote_dir()
    source_root = paths.get_attachments_dir() / "avibe"
    source_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    source_root.chmod(0o700)
    return AttachmentPinStore(
        root=home / "memory" / "attachments",
        source_root=source_root,
    )


def _source_attachment(name: str, payload: bytes = b"attachment payload") -> CaptureAttachment:
    source_root = paths.get_attachments_dir() / "avibe"
    source_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    source_root.chmod(0o700)
    source = source_root / name
    source.write_bytes(payload)
    source.chmod(0o600)
    extension = source.suffix.lstrip(".").lower()
    return CaptureAttachment(
        kind="image" if extension == "png" else "doc",
        name=source.name,
        uri=source.as_uri(),
        ext=extension,
    )


def _write_owned_provider_root(root: Path, store: MemoryStore) -> None:
    meta = store.ensure_meta()
    ProviderRoot(
        root,
        effective_home=paths.get_vibe_remote_dir(),
    ).ensure(
        meta,
        ProviderRootMetadata(
            provider_root_format="slice1",
            compatible_provider_root_formats=frozenset({"slice1"}),
            artifact_fingerprint="slice1-core",
        ),
    )


def _request(
    *,
    source: str = "source-1",
    session: str = "conversation-1",
    text: str = "remember this",
    occurred_at_ms: int = 1_000,
    attachments: tuple[CaptureAttachment, ...] = (),
    principal_id: str = PRINCIPAL,
    project_id: str = PROJECT,
):
    from core.memory.types import CaptureRequest

    return CaptureRequest(
        source_message_id=source,
        session_id=session,
        principal_id=principal_id,
        project_id=project_id,
        provenance="user_input",
        text=text,
        occurred_at_ms=occurred_at_ms,
        attachments=attachments,
    )


def _module(
    tmp_path: Path,
    *,
    provider: FakeMemoryProvider | None = None,
    enabled=True,
    disk_free_bytes=None,
    owned_provider_root: bool = False,
    **kwargs,
) -> tuple[MemoryModule, MemoryStore, FakeMemoryProvider]:
    store = MemoryStore(_store_path(tmp_path))
    if owned_provider_root:
        provider_root = kwargs.pop(
            "provider_root",
            paths.get_vibe_remote_dir() / "memory" / "provider-root",
        )
        _write_owned_provider_root(provider_root, store)
        kwargs["provider_root"] = provider_root
    kwargs.setdefault("attachment_store", _attachment_store())
    fake = provider or FakeMemoryProvider()
    module = MemoryModule(
        store,
        fake,
        enabled=enabled,
        disk_free_bytes=disk_free_bytes or (lambda: MIN_FREE_DISK_BYTES),
        **kwargs,
    )
    return module, store, fake


def _set_embed_capability(provider: FakeMemoryProvider, available: bool) -> None:
    provider.health_snapshot_value = replace(
        provider.health_snapshot_value,
        capabilities={
            **provider.health_snapshot_value.capabilities,
            "embed": available,
        },
    )


async def test_disabled_capture_and_reads_are_closed_without_creating_state(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path, enabled=False)

    assert await module.capture(_request()) == CaptureSkipped(reason="memory_disabled")
    assert await module.search("query", principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_disabled"
    )
    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_disabled"
    )
    assert store.get_meta() is None


async def test_maintenance_fence_closes_capture_and_reads(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path, maintenance_open=lambda: True)

    assert await module.capture(_request()) == CaptureSkipped(reason="memory_clear_failed")
    assert await module.recall(
        "query",
        policy=RecallPolicy(mode="keyword"),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    ) == OperationFailed(error="memory_clear_failed")
    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_clear_failed"
    )
    assert store.list_queue_rows() == ()
    assert provider.search_scopes == []


async def test_module_maintenance_state_fences_capture_until_closed(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)

    module.enter_maintenance()
    assert module.maintenance_active
    assert await module.capture(_request()) == CaptureSkipped(reason="memory_clear_failed")
    assert store.list_queue_rows() == ()

    module.leave_maintenance()
    assert not module.maintenance_active
    assert await module.capture(_request()) == CaptureAccepted()


async def test_destructive_lifecycle_owns_root_and_blocks_ordinary_lifecycle(
    tmp_path: Path,
) -> None:
    module, _store, _provider = _module(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def destructive_operation() -> None:
        async with module.destructive_lifecycle():
            entered.set()
            async with module.observe_provider_root() as root_available:
                assert root_available is False
            await release.wait()

    destructive = asyncio.create_task(destructive_operation())
    ordinary: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        ordinary_entered = False

        async def ordinary_operation() -> None:
            nonlocal ordinary_entered
            async with module.lifecycle():
                ordinary_entered = True

        ordinary = asyncio.create_task(ordinary_operation())
        await asyncio.sleep(0)
        assert ordinary_entered is False

        release.set()
        await destructive
        await ordinary
        assert ordinary_entered is True
    finally:
        release.set()
        await asyncio.gather(
            destructive,
            *((ordinary,) if ordinary is not None else ()),
            return_exceptions=True,
        )


async def test_claim_quiescence_fences_delivery_until_resumed(tmp_path: Path) -> None:
    module, _store, provider = _module(tmp_path)
    assert await module.capture(_request()) == CaptureAccepted()

    assert await module.quiesce_claims()
    assert await module.drain() == 0
    assert provider.captures == []

    module.resume_claims()
    assert await module.drain() == 1
    assert [capture.text for capture in provider.captures] == ["remember this"]


async def test_timed_out_quiescence_remains_fenced_until_resumed(tmp_path: Path) -> None:
    add_entered = asyncio.Event()
    release_add = asyncio.Event()

    async def block_add(_capture) -> None:
        add_entered.set()
        await release_add.wait()

    provider = FakeMemoryProvider(add_hook=block_add)
    module, _store, _provider = _module(tmp_path, provider=provider)
    assert await module.capture(_request()) == CaptureAccepted()

    draining = asyncio.create_task(module.drain())
    try:
        await asyncio.wait_for(add_entered.wait(), timeout=1.0)
        assert not await module.quiesce_claims(timeout_seconds=0.01)

        release_add.set()
        assert await draining == 1
        assert await module.capture(_request(source="still-paused")) == CaptureAccepted()
        assert await module.drain() == 0

        module.resume_claims()
        assert await module.drain() == 1
    finally:
        release_add.set()
        await asyncio.gather(draining, return_exceptions=True)


async def test_provider_replacement_updates_reads_and_claim_delivery(tmp_path: Path) -> None:
    original = FakeMemoryProvider()
    replacement = FakeMemoryProvider(
        search_items=(MemoryItem(kind="fact", text="replacement result"),),
    )
    module, _store, _provider = _module(tmp_path, provider=original)

    module.replace_provider(replacement)
    assert await module.search(
        "query",
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    ) == MemoryItems(items=replacement.search_items)
    assert await module.capture(_request()) == CaptureAccepted()
    assert await module.drain() == 1
    assert original.captures == []
    assert [capture.text for capture in replacement.captures] == ["remember this"]


async def test_session_lifecycle_fences_capture_through_operation(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    operation_entered = asyncio.Event()
    release_operation = asyncio.Event()

    async def reset_session() -> str:
        operation_entered.set()
        await release_operation.wait()
        return "reset-complete"

    lifecycle = asyncio.create_task(
        module.run_session_lifecycle(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            raw_session_id="conversation-1",
            operation=reset_session,
            deadline_seconds=2.0,
        )
    )
    capture: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(operation_entered.wait(), timeout=1.0)
        capture = asyncio.create_task(module.capture(_request(source="after-reset")))
        await asyncio.sleep(0)

        assert not capture.done()
        assert store.list_queue_rows() == ()

        release_operation.set()
        assert await lifecycle == "reset-complete"
        assert await capture == CaptureAccepted()
    finally:
        release_operation.set()
        await asyncio.gather(
            lifecycle,
            *((capture,) if capture is not None else ()),
            return_exceptions=True,
        )


async def test_capture_normalizes_deduplicates_and_never_persists_raw_ids(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    request = _request(
        source="raw-source-id-canary",
        session="raw-session-id-canary",
        text="Cafe\u0301\r\nmessage",
        occurred_at_ms=5_000,
    )

    assert await module.capture(request) == CaptureAccepted()
    assert await module.capture(request) == CaptureDuplicate()
    rows = store.list_queue_rows()

    assert len(rows) == 1
    assert rows[0].payload_text == "Café\nmessage"
    assert rows[0].session_id.startswith("src--")
    assert "raw-session-id-canary" not in rows[0].session_id
    with sqlite3.connect(store.path) as conn:
        dump = "\n".join(
            str(value)
            for row in conn.execute("SELECT * FROM memory_capture_queue")
            for value in row
        )
    assert "raw-source-id-canary" not in dump
    assert "raw-session-id-canary" not in dump


async def test_capture_pins_a_real_attachment_and_forwards_the_private_copy(tmp_path: Path) -> None:
    attachment_store = _attachment_store()
    module, store, provider = _module(tmp_path, attachment_store=attachment_store)
    attachment = _source_attachment("diagram.png", b"real image bytes")
    source_path = Path(attachment.uri.removeprefix("file://"))

    assert await module.capture(_request(attachments=(attachment,))) == CaptureAccepted(
        captured_attachment_count=1
    )
    queued = store.list_queue_rows()[0]
    assert queued.payload_attachments is not None
    assert queued.attachment_bundle_id is not None
    assert attachment.uri not in queued.payload_attachments

    source_path.unlink()
    assert await module.drain() == 1
    forwarded = provider.captures[0].attachments[0]
    assert forwarded.name == attachment.name
    assert forwarded.uri != attachment.uri
    assert store.list_queue_rows()[0].payload_attachments is None


async def test_capture_pin_failure_preserves_mixed_turn_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    attachment_store = _attachment_store()
    module, store, _provider = _module(tmp_path, attachment_store=attachment_store)
    attachment = _source_attachment("changed.png", b"original bytes")

    def fail_pin(*_args, **_kwargs):
        raise AttachmentPinError(
            "memory_store_unavailable",
            "leased source changed before pinning",
        )

    monkeypatch.setattr(attachment_store, "pin", fail_pin)
    mixed = replace(
        _request(source="mixed-pin-failure", attachments=(attachment,)),
        text="keep this caption",
        attachment_config_generation=7,
    )
    assert await module.capture(mixed) == CaptureAccepted()

    rows = store.list_queue_rows()
    assert len(rows) == 1
    assert rows[0].payload_text == "keep this caption"
    assert rows[0].payload_attachments is None
    assert rows[0].attachment_bundle_id is None
    attachment_only = replace(
        mixed,
        source_message_id="attachment-only-pin-failure",
        text="",
    )
    assert await module.capture(attachment_only) == OperationFailed(
        error="memory_store_unavailable"
    )
    assert len(store.list_queue_rows()) == 1


async def test_unexpected_capture_pin_failure_preserves_mixed_turn_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario: MEMORY-IM-ATTACH-004."""

    attachment_store = _attachment_store()
    module, store, _provider = _module(tmp_path, attachment_store=attachment_store)
    attachment = _source_attachment("unexpected.png", b"original bytes")

    def fail_pin(*_args, **_kwargs):
        raise RuntimeError("unexpected pin failure")

    monkeypatch.setattr(attachment_store, "pin", fail_pin)
    mixed = replace(
        _request(source="unexpected-pin-failure", attachments=(attachment,)),
        text="keep this caption",
        attachment_config_generation=7,
    )

    assert await module.capture(mixed) == CaptureAccepted()
    rows = store.list_queue_rows()
    assert len(rows) == 1
    assert rows[0].payload_text == "keep this caption"
    assert rows[0].payload_attachments is None
    assert rows[0].attachment_bundle_id is None


async def test_boot_reconcile_waits_for_attachment_admission_and_preserves_accepted_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = _attachment_store()
    module, store, _provider = _module(tmp_path, attachment_store=attachment_store)
    attachment = _source_attachment("activation-race.txt", b"accepted bytes")
    published = threading.Event()
    allow_pin_return = threading.Event()
    recovery_started = threading.Event()
    pinned: list[PinnedBundle] = []
    original_pin = attachment_store.pin
    original_recover = store.recover_after_boot

    def pin_before_enqueue(sources):
        bundle = original_pin(sources)
        pinned.append(bundle)
        published.set()
        if not allow_pin_return.wait(timeout=2):
            raise AssertionError("capture admission was not released")
        return bundle

    def observe_recovery(*, lease_owner, clock):
        recovery_started.set()
        return original_recover(lease_owner=lease_owner, clock=clock)

    monkeypatch.setattr(attachment_store, "pin", pin_before_enqueue)
    monkeypatch.setattr(store, "recover_after_boot", observe_recovery)

    capture = asyncio.create_task(
        module.capture(_request(source="activation-race", attachments=(attachment,)))
    )
    try:
        assert await asyncio.to_thread(published.wait, 1)
        module.pause_claims()
        module.begin_activation(new_lease=True)
        recovery = asyncio.create_task(module.drain())
        assert await asyncio.to_thread(recovery_started.wait, 1)

        completed, _pending = await asyncio.wait({recovery}, timeout=0.1)
        assert completed == set()

        allow_pin_return.set()
        assert await capture == CaptureAccepted(captured_attachment_count=1)
        await recovery
    finally:
        allow_pin_return.set()

    bundle = pinned[0]
    assert attachment_store.provider_attachments(bundle)[0].name == attachment.name
    assert store.list_queue_rows()[0].attachment_bundle_id == bundle.bundle_id

    restarted_store = MemoryStore(store.path)
    restarted_attachments = _attachment_store()
    restarted = MemoryModule(
        restarted_store,
        FakeMemoryProvider(),
        enabled=True,
        attachment_store=restarted_attachments,
    )
    restarted.pause_claims()
    restarted.begin_activation(new_lease=True)
    await restarted.drain()

    assert restarted_attachments.provider_attachments(bundle)[0].name == attachment.name
    assert restarted_store.list_queue_rows()[0].attachment_bundle_id == bundle.bundle_id


async def test_capture_validation_and_disk_rejections_increment_only_missed(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path, disk_free_bytes=lambda: 0)
    source = _source_attachment("validation.txt")
    oversized_attachments = tuple(
        CaptureAttachment(
            kind="doc",
            name=f"{'x' * 2_100}-{index}.txt",
            uri=source.uri,
            ext="txt",
        )
        for index in range(8)
    )

    blank = await module.capture(_request(text="\r\n  \r"))
    command = await module.capture(_request(source="source-2", text="/memory search private"))
    too_large = await module.capture(_request(source="source-3", text="x" * (MAX_CAPTURE_TEXT_BYTES + 1)))
    oversized_id = await module.capture(
        _request(source="x" * (MAX_CAPTURE_IDENTIFIER_BYTES + 1), text="content")
    )
    oversized_metadata = await module.capture(
        _request(source="source-attachments", attachments=oversized_attachments)
    )
    invalid_unicode = await module.capture(
        _request(
            source="source-unicode",
            attachments=(
                CaptureAttachment(kind="doc", name="\ud800.txt", uri=source.uri, ext="txt"),
            ),
        )
    )
    disk = await module.capture(_request(source="source-4", text="content"))

    assert blank == CaptureSkipped(reason="memory_invalid_input")
    assert command == CaptureSkipped(reason="memory_low_disk_space")
    assert too_large == CaptureSkipped(reason="memory_input_too_large")
    assert oversized_id == CaptureSkipped(reason="memory_invalid_input")
    assert oversized_metadata == CaptureSkipped(reason="memory_input_too_large")
    assert invalid_unicode == CaptureSkipped(reason="memory_invalid_input")
    assert disk == CaptureSkipped(reason="memory_low_disk_space")
    assert store.ensure_meta().missed_count == 7
    assert store.ensure_meta().last_error == "memory_low_disk_space"
    assert store.list_queue_rows() == ()


async def test_provider_timestamp_is_allocated_once_for_each_accepted_capture(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)
    first = _request(source="first", occurred_at_ms=5_000)
    second = _request(source="second", occurred_at_ms=5_000)

    assert await module.capture(first) == CaptureAccepted()
    assert await module.capture(second) == CaptureAccepted()
    assert await module.capture(first) == CaptureDuplicate()
    assert [row.provider_timestamp_ms for row in store.list_queue_rows()] == [5_000, 5_001]


async def test_search_and_profile_enforce_bounds_and_return_closed_errors(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        search_items=(MemoryItem(kind="fact", text="bounded fact", date="2026-01-01"),),
        profile_items=(MemoryItem(kind="profile", text="bounded profile"),),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.search("query", principal_id=PRINCIPAL, project_id=PROJECT) == MemoryItems(
        items=provider.search_items
    )
    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == MemoryItems(
        items=provider.profile_items
    )
    assert provider.search_scopes == [(PRINCIPAL, PROJECT)]
    assert provider.profile_scopes == [(PRINCIPAL, PROJECT)]
    assert await module.search(
        "x" * (MAX_QUERY_BYTES + 1),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    ) == OperationFailed(error="memory_input_too_large")

    provider.search_items = tuple(MemoryItem(kind="fact", text=str(index)) for index in range(9))
    assert await module.search("query", principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_provider_response_invalid"
    )
    provider.search_items = ()
    provider.search_failure = RuntimeError("provider-search-body-canary")
    result = await module.search("query", principal_id=PRINCIPAL, project_id=PROJECT)
    assert result == OperationFailed(error="memory_processing_failed")
    assert "provider-search-body-canary" not in repr(result)


async def test_profile_bounds_accept_structured_data_only_on_profile_items(tmp_path: Path) -> None:
    profile = MemoryProfile(
        summary="Uses concise updates.",
        explicit_info=(MemoryProfileExplicitInfo(description="Uses Python."),),
        implicit_traits=(
            MemoryProfileTrait(
                description="May prefer checklists.",
                basis="Repeated planning requests.",
                evidence="Recent project discussions.",
            ),
        ),
        updated_at="2026-08-02T10:30:00Z",
    )
    provider = FakeMemoryProvider(
        profile_items=(MemoryItem(kind="profile", text="{}", profile=profile),),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == MemoryItems(
        items=provider.profile_items
    )

    provider.profile_items = (MemoryItem(kind="fact", text="{}", profile=profile),)
    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_provider_response_invalid"
    )
    provider.profile_items = (
        MemoryItem(kind="profile", text="{}", profile=MemoryProfile(summary="bad\x00value")),
    )
    assert await module.profile(principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_provider_response_invalid"
    )


async def test_list_episodes_enforces_scope_pagination_and_page_bounds(tmp_path: Path) -> None:
    item = MemoryListItem(
        id="opaque-episode-id",
        subject="Subject",
        summary="Summary",
        body="Processed body",
        timestamp="2026-08-14T02:11:12Z",
        project="notes",
    )
    provider = FakeMemoryProvider(
        list_page=MemoryListPage(
            items=(item,),
            page=1,
            page_size=20,
            count=1,
            total_count=6,
        )
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
        page=2,
        page_size=5,
    ) == MemoryListPage(
        items=(item,),
        page=2,
        page_size=5,
        count=1,
        total_count=6,
    )
    assert provider.list_requests == [(PRINCIPAL, "notes", 2, 5)]

    for page, page_size in ((0, 5), (1, 0), (1, 21), (True, 5)):
        assert await module.list_episodes(
            principal_id=PRINCIPAL,
            project_id="notes",
            page=page,
            page_size=page_size,
        ) == OperationFailed(error="memory_invalid_input")
    assert provider.list_requests == [(PRINCIPAL, "notes", 2, 5)]

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="all",
    ) == OperationFailed(error="memory_access_denied")

    provider.list_page = replace(provider.list_page, count=0)
    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
    ) == OperationFailed(error="memory_provider_response_invalid")

    provider.list_failure = RuntimeError("provider-list-body-canary")
    result = await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
    )
    assert result == OperationFailed(error="memory_processing_failed")
    assert "provider-list-body-canary" not in repr(result)


async def test_list_episodes_rejects_nonempty_page_beyond_total_count(
    tmp_path: Path,
) -> None:
    provider = FakeMemoryProvider(
        list_page=MemoryListPage(
            items=(
                MemoryListItem(
                    id="opaque-episode-id",
                    subject="Subject",
                    summary="Summary",
                    body="Processed body",
                    timestamp="2026-08-14T02:11:12Z",
                    project="notes",
                ),
            ),
            page=1,
            page_size=20,
            count=1,
            total_count=1,
        )
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
        page=3,
        page_size=5,
    ) == OperationFailed(error="memory_provider_response_invalid")


@pytest.mark.parametrize(
    "page",
    [
        MemoryListPage(
            items=(),
            page=1,
            page_size=20,
            count=0,
            total_count=1,
        ),
        MemoryListPage(
            items=(
                MemoryListItem(
                    id="opaque-episode-id",
                    subject="Subject",
                    summary="Summary",
                    body="Processed body",
                    timestamp="2026-08-14T02:11:12Z",
                    project="notes",
                ),
            ),
            page=1,
            page_size=20,
            count=1,
            total_count=1,
            status="failed",  # type: ignore[arg-type]
        ),
        MemoryListPage(
            items=(
                MemoryListItem(
                    id="duplicate-id",
                    subject="Subject",
                    summary="Summary",
                    body="Processed body one",
                    timestamp="2026-08-14T02:11:12Z",
                    project="notes",
                ),
                MemoryListItem(
                    id="duplicate-id",
                    subject="Subject",
                    summary="Summary",
                    body="Processed body two",
                    timestamp="2026-08-14T02:11:11Z",
                    project="notes",
                ),
            ),
            page=1,
            page_size=20,
            count=2,
            total_count=2,
        ),
    ],
)
async def test_list_episodes_rejects_invalid_page_envelope(
    tmp_path: Path,
    page: MemoryListPage,
) -> None:
    module, _store, _provider = _module(
        tmp_path,
        provider=FakeMemoryProvider(list_page=page),
    )

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
    ) == OperationFailed(error="memory_provider_response_invalid")


@pytest.mark.parametrize(
    "items",
    [
        (
            MemoryListItem(
                id="older",
                subject="Subject",
                summary="Summary",
                body="Processed body",
                timestamp="2026-08-14T02:11:11Z",
                project="notes",
            ),
            MemoryListItem(
                id="newer",
                subject="Subject",
                summary="Summary",
                body="Processed body",
                timestamp="2026-08-14T02:11:12Z",
                project="notes",
            ),
        ),
        (
            MemoryListItem(
                id="week-date",
                subject="Subject",
                summary="Summary",
                body="Processed body",
                timestamp="2026-W33-5T02:11:12+00:00",
                project="notes",
            ),
        ),
        (
            MemoryListItem(
                id="overflowing-offset",
                subject="Subject",
                summary="Summary",
                body="Processed body",
                timestamp="0001-01-01T00:00:00+23:59",
                project="notes",
            ),
        ),
    ],
)
async def test_list_episodes_rejects_invalid_shared_timestamp_contract(
    tmp_path: Path,
    items: tuple[MemoryListItem, ...],
) -> None:
    provider = FakeMemoryProvider(
        list_page=MemoryListPage(
            items=items,
            page=1,
            page_size=20,
            count=len(items),
            total_count=len(items),
        )
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
    ) == OperationFailed(error="memory_provider_response_invalid")


async def test_list_episodes_rejects_invalid_items_container(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(
        list_page=MemoryListPage(
            items=None,  # type: ignore[arg-type]
            page=1,
            page_size=20,
            count=0,
            total_count=0,
        )
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="notes",
    ) == OperationFailed(error="memory_provider_response_invalid")


async def test_list_episodes_requires_canonical_provider_text(tmp_path: Path) -> None:
    item = MemoryListItem(
        id="opaque-episode-id",
        subject="Subject",
        summary="Summary",
        body="Processed body",
        timestamp="2026-08-14T02:11:12Z",
        project="notes",
    )
    provider = FakeMemoryProvider()
    module, _store, _provider = _module(tmp_path, provider=provider)

    for malformed in (
        replace(item, subject=" Subject"),
        replace(item, summary="Summary "),
        replace(item, body=" \t "),
    ):
        provider.list_page = MemoryListPage(
            items=(malformed,),
            page=1,
            page_size=20,
            count=1,
            total_count=1,
        )
        assert await module.list_episodes(
            principal_id=PRINCIPAL,
            project_id="notes",
        ) == OperationFailed(error="memory_provider_response_invalid")


async def test_keyword_recall_skips_health_and_succeeds_without_embedding(tmp_path: Path) -> None:
    class NoHealthProvider(FakeMemoryProvider):
        async def health_snapshot(self):
            raise AssertionError("keyword recall must not probe health")

    provider = NoHealthProvider(
        search_items=(MemoryItem(kind="fact", text="keyword result"),),
    )
    _set_embed_capability(provider, False)
    module, _store, _provider = _module(tmp_path, provider=provider)

    result = await module.recall(
        "exact term",
        policy=RecallPolicy(mode="keyword", include_profile=False),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == RecallItems(
        items=provider.search_items,
        requested_mode="keyword",
        effective_mode="keyword",
    )
    assert provider.search_policies == [("keyword", False, None)]


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_explicit_embedding_modes_fail_closed_without_search(
    tmp_path: Path,
    mode: str,
) -> None:
    provider = FakeMemoryProvider()
    _set_embed_capability(provider, False)
    module, _store, _provider = _module(tmp_path, provider=provider)

    result = await module.recall(
        "query",
        policy=RecallPolicy(mode=mode),  # type: ignore[arg-type]
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []
    assert provider.search_policies == []


@pytest.mark.parametrize(
    ("embed_available", "effective_mode"),
    [(False, "keyword"), (True, "hybrid")],
)
async def test_auto_recall_selects_only_keyword_or_hybrid(
    tmp_path: Path,
    embed_available: bool,
    effective_mode: str,
) -> None:
    provider = FakeMemoryProvider(search_items=(MemoryItem(kind="fact", text="result"),))
    _set_embed_capability(provider, embed_available)
    module, _store, _provider = _module(tmp_path, provider=provider)

    result = await module.recall(
        "query",
        policy=RecallPolicy(mode="auto"),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert isinstance(result, RecallItems)
    assert result.requested_mode == "auto"
    assert result.effective_mode == effective_mode
    assert provider.search_policies == [(effective_mode, True, None)]


@pytest.mark.parametrize("missing_capability", ["embed", "llm", "rerank"])
async def test_agentic_recall_fails_closed_when_a_capability_is_missing(
    tmp_path: Path,
    missing_capability: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeMemoryProvider(agentic_budget_enforced_flag=True)
    provider.health_snapshot_value = replace(
        provider.health_snapshot_value,
        capabilities={
            **provider.health_snapshot_value.capabilities,
            missing_capability: False,
        },
    )
    module, _store, _provider = _module(tmp_path, provider=provider)
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=5,
        max_model_calls=1,
        cost_budget_tokens=1_000,
    )

    caplog.set_level("INFO", logger="core.memory.module")
    result = await module.recall(
        "query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []
    assert provider.search_policies == []
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "mode=agentic" in telemetry[0]
    assert "round=unknown" in telemetry[0]
    assert "success=false" in telemetry[0]
    assert "timeout=false" in telemetry[0]
    assert "query" not in telemetry[0]


async def test_agentic_recall_fails_closed_when_health_disables_agentic_search(
    tmp_path: Path,
) -> None:
    provider = FakeMemoryProvider(agentic_budget_enforced_flag=True)
    provider.health_snapshot_value = replace(
        provider.health_snapshot_value,
        disabled_features=("agentic_search",),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=5,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )

    result = await module.recall(
        "query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []


async def test_agentic_mode_resolution_bounds_the_capability_probe(
    tmp_path: Path,
) -> None:
    provider = FakeMemoryProvider(agentic_budget_enforced_flag=True)
    module, _store, _provider = _module(tmp_path, provider=provider)

    async def slow_health_snapshot():
        await asyncio.sleep(1)
        return provider.health_snapshot_value

    provider.health_snapshot = slow_health_snapshot  # type: ignore[method-assign]
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=5,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )

    result = await module.resolve_recall_mode(policy, timeout_seconds=0.001)

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []


async def test_agentic_recall_logs_capability_probe_timeout(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeMemoryProvider(agentic_budget_enforced_flag=True)
    module, _store, _provider = _module(tmp_path, provider=provider)

    async def slow_health_snapshot():
        await asyncio.sleep(1)
        return provider.health_snapshot_value

    provider.health_snapshot = slow_health_snapshot  # type: ignore[method-assign]
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=0.01,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )

    caplog.set_level("INFO", logger="core.memory.module")
    result = await module.recall(
        "private query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_provider_timeout")
    assert provider.search_scopes == []
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "mode=agentic" in telemetry[0]
    assert "success=false" in telemetry[0]
    assert "timeout=true" in telemetry[0]
    assert "private query" not in telemetry[0]


async def test_agentic_recall_logs_typed_provider_health_timeout(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeMemoryProvider(
        agentic_budget_enforced_flag=True,
        health_failure=MemoryProviderFailure("memory_provider_timeout"),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=30,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )

    caplog.set_level("INFO", logger="core.memory.module")
    result = await module.recall(
        "private query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "mode=agentic" in telemetry[0]
    assert "success=false" in telemetry[0]
    assert "timeout=true" in telemetry[0]
    assert "private query" not in telemetry[0]


async def test_agentic_recall_reaches_provider_with_policy_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeMemoryProvider(
        agentic_budget_enforced_flag=True,
        agentic_round="round2",
        search_items=(MemoryItem(kind="fact", text="agentic result"),),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=5,
        max_model_calls=2,
        cost_budget_tokens=32_000,
    )
    monotonic_values = iter((100.0, 100.0, 100.0, 102.0, 102.0))
    monkeypatch.setattr(
        "core.memory.module.monotonic",
        lambda: next(monotonic_values),
    )
    resolve_timeouts: list[float | None] = []
    resolve_recall_mode = module.resolve_recall_mode

    async def tracked_resolve_recall_mode(
        tracked_policy: RecallPolicy,
        *,
        timeout_seconds: float | None = None,
        agentic_telemetry=None,
    ):
        resolve_timeouts.append(timeout_seconds)
        return await resolve_recall_mode(
            tracked_policy,
            timeout_seconds=timeout_seconds,
            agentic_telemetry=agentic_telemetry,
        )

    monkeypatch.setattr(module, "resolve_recall_mode", tracked_resolve_recall_mode)

    caplog.set_level("INFO", logger="core.memory.module")
    result = await module.recall(
        "private query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == RecallItems(
        items=provider.search_items,
        requested_mode="agentic",
        effective_mode="agentic",
    )
    assert provider.search_policies == [("agentic", True, None)]
    assert resolve_timeouts == [5.0]
    assert provider.search_timeouts == [3.0]
    telemetry = [
        record.getMessage()
        for record in caplog.records
        if "telemetry" in record.getMessage()
    ]
    assert len(telemetry) == 1
    assert "mode=agentic" in telemetry[0]
    assert "round=round2" in telemetry[0]
    assert "duration_ms=2000" in telemetry[0]
    assert "success=true" in telemetry[0]
    assert "timeout=false" in telemetry[0]
    assert "private query" not in telemetry[0]


async def test_current_session_overlay_uses_the_trusted_canonical_reference(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(search_items=(MemoryItem(kind="episode", text="current turn"),))
    module, store, _provider = _module(tmp_path, provider=provider)
    expected_ref = store.provider_session_ref(
        principal_id=PRINCIPAL,
        project_ref=PROJECT,
        session_id="trusted-current-session",
    )

    result = await module.recall(
        "query",
        policy=RecallPolicy(mode="keyword", include_current_session=True),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        current_session_id="trusted-current-session",
    )

    assert isinstance(result, RecallItems)
    assert result.current_session_overlay is True
    assert provider.search_policies == [("keyword", True, expected_ref)]
    assert expected_ref.principal_id == PRINCIPAL
    assert expected_ref.project_ref == PROJECT
    assert expected_ref.session_id != "trusted-current-session"


async def test_current_session_overlay_requires_trusted_context_before_search(tmp_path: Path) -> None:
    module, _store, provider = _module(tmp_path)

    result = await module.recall(
        "query",
        policy=RecallPolicy(mode="keyword", include_current_session=True),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_invalid_input")
    assert provider.search_scopes == []


async def test_recall_makes_one_search_and_never_falls_back(tmp_path: Path) -> None:
    provider = FakeMemoryProvider(search_failure=MemoryProviderFailure("memory_processing_failed"))
    _set_embed_capability(provider, True)
    module, _store, _provider = _module(tmp_path, provider=provider)

    result = await module.recall(
        "query",
        policy=RecallPolicy(mode="hybrid"),
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_processing_failed")
    assert provider.search_scopes == [(PRINCIPAL, PROJECT)]
    assert provider.search_policies == [("hybrid", True, None)]


async def test_failure_log_is_a_bounded_read_without_creating_state(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)

    assert await module.failure_log(limit=1) == ()
    assert store.get_meta() is None


async def test_memory_never_logs_or_serializes_capture_or_provider_canaries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "very-secret-user-text-canary"
    provider = FakeMemoryProvider(search_failure=RuntimeError("provider-error-body-canary"))
    module, _store, _provider = _module(tmp_path, provider=provider)

    assert await module.capture(_request(text=canary)) == CaptureAccepted()
    result = await module.search("query-canary", principal_id=PRINCIPAL, project_id=PROJECT)

    rendered = f"{result!r}\n{caplog.text}"
    assert canary not in rendered
    assert "provider-error-body-canary" not in rendered


async def test_provider_failures_are_closed_codes_and_never_expose_provider_text(tmp_path: Path) -> None:
    canary = "https://provider.invalid/v1?api_key=memory-key-canary"
    provider = FakeMemoryProvider(search_failure=MemoryProviderFailure(canary))  # type: ignore[arg-type]
    module, _store, _provider = _module(tmp_path, provider=provider)

    result = await module.search("query", principal_id=PRINCIPAL, project_id=PROJECT)

    assert isinstance(result, OperationFailed)
    assert result.error in CLOSED_MEMORY_ERROR_CODES
    assert canary not in repr(result)


async def test_malformed_unicode_returns_closed_capture_and_search_errors(tmp_path: Path) -> None:
    module, _store, _provider = _module(tmp_path)

    assert await module.capture(_request(text="\ud800")) == CaptureSkipped(
        reason="memory_invalid_input"
    )
    assert await module.search("\ud800", principal_id=PRINCIPAL, project_id=PROJECT) == OperationFailed(
        error="memory_invalid_input"
    )


async def test_capture_happy_path_uses_one_local_queue_transaction(tmp_path: Path) -> None:
    class CountingStore(MemoryStore):
        def __init__(self, path: Path) -> None:
            self.transactions = 0
            super().__init__(path)

        @contextmanager
        def _transaction(self):
            self.transactions += 1
            with super()._transaction() as conn:
                yield conn

    store = CountingStore(_store_path(tmp_path))
    module = MemoryModule(
        store,
        FakeMemoryProvider(),
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        attachment_store=_attachment_store(),
    )
    store.transactions = 0

    assert await module.capture(_request()) == CaptureAccepted()
    assert store.transactions == 1


async def test_capture_identifiers_are_validated_before_enqueue(tmp_path: Path) -> None:
    module, store, _provider = _module(tmp_path)

    assert await module.capture(_request(source="   ")) == CaptureSkipped(reason="memory_invalid_input")
    assert await module.capture(_request(session="\t\n")) == CaptureSkipped(
        reason="memory_invalid_input"
    )
    assert await module.capture(_request(principal_id="avibe:local")) == CaptureSkipped(
        reason="memory_invalid_input"
    )
    assert store.ensure_meta().missed_count == 3


def test_provider_port_is_not_part_of_the_public_memory_package() -> None:
    import core.memory as memory

    assert "MemoryProviderPort" not in memory.__all__
    assert "ProviderCapture" not in memory.__all__


def test_memory_list_result_types_are_public() -> None:
    import core.memory as memory

    assert memory.MemoryListItem is MemoryListItem
    assert memory.MemoryListPage is MemoryListPage
    assert memory.MemoryListResult is MemoryListResult
    assert {"MemoryListItem", "MemoryListPage", "MemoryListResult"} <= set(
        memory.__all__
    )


def test_slice2_runtime_types_remain_internal_to_the_memory_package() -> None:
    import core.memory as memory
    import core.memory.artifact as artifact
    import core.memory.process as process

    assert hasattr(artifact, "MemoryArtifactManager")
    assert hasattr(process, "EverOSProcess")
    assert "MemoryArtifactManager" not in memory.__all__
    assert "EverOSProcess" not in memory.__all__
