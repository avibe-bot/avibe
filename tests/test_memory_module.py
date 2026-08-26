"""Focused Memory module tests for the bounded best-effort writer."""

import asyncio
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import core.memory.module as memory_module
from core.memory.attachments import (
    AttachmentCleanupUnprovenError,
    AttachmentPinError,
    AttachmentPinStore,
)
from core.memory.everos import FakeMemoryProvider
from core.memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from core.memory.retained_input import RetainedInputBudget
from core.memory.store import MemoryStore, VolatileAdmission
from core.memory.types import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureRequest,
    CaptureSkipped,
    MemoryItem,
    MemoryItems,
    MemoryListItem,
    MemoryListPage,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    OperationFailed,
    ProviderSearchItem,
)
from core.memory.writer import MAX_WRITER_PERMITS

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


def _module(
    tmp_path: Path,
    *,
    provider: FakeMemoryProvider | None = None,
) -> tuple[MemoryModule, MemoryStore, FakeMemoryProvider]:
    store = MemoryStore(tmp_path / "state" / "memory" / "memory.sqlite", effective_home=tmp_path)
    provider = provider or FakeMemoryProvider()
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
    """MEMORY-SEARCH-013: duplicate suppression is process-local and volatile."""

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
    """MEMORY-SEARCH-017: a lifecycle barrier is offered without a drain."""

    module, _store, _provider = _module(tmp_path)
    module._writer._ensure_worker = lambda: None
    await module.capture(_request())
    assert module.offer_barrier("session-1") == "queued"
    await module.close_writer()


@pytest.mark.asyncio
async def test_agent_remember_round_trips_through_dual_owner_search(
    tmp_path: Path,
) -> None:
    """MEMORY-SEARCH-016: Agent remember is returned by dual-owner search."""

    class CapturedSearchProvider(FakeMemoryProvider):
        async def search(
            self,
            principal_id,
            project_id,
            query,
            limit,
            **_options,
        ):
            return tuple(
                ProviderSearchItem(
                    item=MemoryItem(kind="episode", text=capture.text),
                    score=1.0,
                    episode_id=f"captured-{index}",
                    timestamp=None,
                    provider_rank=index,
                    queried_owner=principal_id,
                )
                for index, capture in enumerate(self.captures)
                if capture.session_ref.principal_id == principal_id
                and capture.session_ref.project_ref == project_id
                and query.casefold() in capture.text.casefold()
            )[:limit]

    provider = CapturedSearchProvider()
    module, _store, _provider = _module(tmp_path, provider=provider)
    remembered = "The user plans the release on the 23rd"

    assert await module.capture(
        _request(
            source_message_id="agent-remember-round-trip",
            text=remembered,
            provenance="agent",
        )
    ) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()

    result = await module.search(
        "23rd",
        principal_id=PRINCIPAL,
        project_id="default",
    )

    assert [capture.session_ref.principal_id for capture in provider.captures] == [
        f"{PRINCIPAL}-agent"
    ]
    assert result == MemoryItems(
        items=(MemoryItem(kind="episode", text=remembered, origin="agent"),)
    )


@pytest.mark.asyncio
async def test_large_provider_payloads_cross_capture_search_and_profile_boundaries(
    tmp_path: Path,
) -> None:
    large_text = "x" * (2 * 1024 * 1024)
    large_episode_id = "episode-" + "e" * 300
    profile = MemoryProfile(
        summary="s" * 70_000,
        explicit_info=tuple(
            MemoryProfileExplicitInfo(description=f"profile item {index}")
            for index in range(201)
        ),
        implicit_traits=(),
        updated_at=None,
    )
    profile_item = MemoryItem(kind="profile", text=large_text, profile=profile)
    provider = FakeMemoryProvider(
        search_items_by_owner={
            PRINCIPAL: (
                ProviderSearchItem(
                    item=MemoryItem(kind="episode", text=large_text),
                    score=1.0,
                    episode_id=large_episode_id,
                    timestamp="2026-08-26T00:00:00Z",
                    provider_rank=0,
                    queried_owner=PRINCIPAL,
                ),
            )
        },
        profile_items_by_owner={
            PRINCIPAL: (profile_item,),
            f"{PRINCIPAL}-agent": (profile_item,),
        },
        list_page=MemoryListPage(
            items=(
                MemoryListItem(
                    id="list-" + "i" * 300,
                    subject="",
                    summary="s" * 70_000,
                    body=large_text,
                    timestamp="2026-08-26T00:00:00Z",
                    project="default",
                ),
            ),
            page=1,
            page_size=1,
            count=1,
            total_count=1,
        ),
    )
    module, _store, _provider = _module(tmp_path, provider=provider)

    capture_text = "记" * 70_000
    assert await module.capture(_request(text=capture_text)) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()
    search = await module.search(
        "q" * 70_000,
        principal_id=PRINCIPAL,
        project_id="default",
    )
    listing = await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="default",
        page_size=1,
    )
    result = await module.profile(principal_id=PRINCIPAL, project_id="default")

    assert provider.captures[0].text == capture_text
    assert isinstance(search, MemoryItems)
    assert [item.origin for item in search.items] == ["user"]
    assert listing == MemoryListPage(
        items=(
            MemoryListItem(
                id="list-" + "i" * 300,
                subject="",
                summary="s" * 70_000,
                body=large_text,
                timestamp="2026-08-26T00:00:00Z",
                project="default",
                origin="user",
            ),
        ),
        page=1,
        page_size=1,
        count=1,
        total_count=1,
    )
    assert isinstance(result, MemoryItems)
    assert [item.origin for item in result.items] == ["user", "agent"]
    assert all(item.profile == profile for item in result.items)


@pytest.mark.asyncio
async def test_concurrent_large_searches_share_retained_input_admission(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSearchProvider(FakeMemoryProvider):
        async def search(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return ()

    module, _store, _provider = _module(
        tmp_path,
        provider=BlockingSearchProvider(),
    )
    module._search_input_budget = RetainedInputBudget(max_bytes=1)

    first = asyncio.create_task(
        module.search(
            "first arbitrarily large query",
            principal_id=PRINCIPAL,
            project_id="default",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    second = await module.search(
        "second arbitrarily large query",
        principal_id=PRINCIPAL,
        project_id="default",
    )

    assert second == OperationFailed(error="memory_queue_full")
    release.set()
    assert await first == MemoryItems()


@pytest.mark.asyncio
async def test_worker_validated_results_skip_controller_text_reencoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    large_text = "x" * (2 * 1024 * 1024 + 1)
    fingerprint = "validated-text-fingerprint"
    profile = MemoryProfile(summary="s" * 70_000)
    profile_item = MemoryItem(
        kind="profile",
        text=large_text,
        profile=profile,
        provider_validated=True,
    )
    provider = FakeMemoryProvider(
        search_items_by_owner={
            PRINCIPAL: (
                ProviderSearchItem(
                    item=MemoryItem(
                        kind="episode",
                        text=large_text,
                        provider_validated=True,
                    ),
                    score=1.0,
                    episode_id="episode-1",
                    timestamp="2026-08-26T00:00:00Z",
                    provider_rank=0,
                    queried_owner=PRINCIPAL,
                    provider_validated=True,
                    text_fingerprint=fingerprint,
                    rank_fingerprint="validated-rank-fingerprint",
                ),
            )
        },
        profile_items_by_owner={
            PRINCIPAL: (profile_item,),
            f"{PRINCIPAL}-agent": (),
        },
    )
    module, _store, _provider = _module(tmp_path, provider=provider)
    original_utf8_bytes = memory_module._utf8_bytes
    original_normalize = MemoryModule._normalize_text

    def guarded_utf8_bytes(value: object):
        if value is large_text:
            pytest.fail("controller re-encoded worker-validated provider text")
        return original_utf8_bytes(value)

    def guarded_normalize(value: object) -> str:
        if value is large_text:
            pytest.fail("controller re-normalized worker-validated provider text")
        return original_normalize(value)

    monkeypatch.setattr(memory_module, "_utf8_bytes", guarded_utf8_bytes)
    monkeypatch.setattr(MemoryModule, "_normalize_text", staticmethod(guarded_normalize))
    monkeypatch.setattr(
        memory_module,
        "_same_method_rank_key",
        lambda _item: pytest.fail(
            "controller sorted provider IDs outside the isolated worker"
        ),
    )
    monkeypatch.setattr(
        memory_module,
        "_profile_bytes",
        lambda _profile: pytest.fail(
            "controller revalidated worker-prepared profile fields"
        ),
    )

    result = await module.search(
        "query",
        principal_id=PRINCIPAL,
        project_id="default",
    )
    profile_result = await module.profile(
        principal_id=PRINCIPAL,
        project_id="default",
    )

    assert result == MemoryItems(
        items=(MemoryItem(kind="episode", text=large_text, origin="user"),)
    )
    assert profile_result == MemoryItems(
        items=(
            MemoryItem(
                kind="profile",
                text=large_text,
                profile=profile,
                origin="user",
            ),
        )
    )


@pytest.mark.asyncio
async def test_worker_validated_list_skips_controller_text_reencoding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    large_text = "x" * (2 * 1024 * 1024 + 1)
    page = MemoryListPage(
        items=(
            MemoryListItem(
                id="episode-1",
                subject="Subject",
                summary=large_text,
                body=large_text,
                timestamp="2026-08-26T00:00:00Z",
                project="default",
                provider_validated=True,
            ),
        ),
        page=1,
        page_size=1,
        count=1,
        total_count=1,
        provider_validated=True,
    )
    module, _store, _provider = _module(
        tmp_path,
        provider=FakeMemoryProvider(list_page=page),
    )
    monkeypatch.setattr(
        memory_module,
        "_list_text_bytes",
        lambda *_args, **_kwargs: pytest.fail(
            "controller re-encoded worker-validated list text"
        ),
    )

    result = await module.list_episodes(
        principal_id=PRINCIPAL,
        project_id="default",
        page_size=1,
    )

    assert isinstance(result, MemoryListPage)
    assert result.items[0].summary == large_text
    assert result.items[0].body == large_text
    assert result.items[0].origin == "user"
    assert result.items[0].provider_validated is True
    assert result.provider_validated is True


def test_worker_validated_rank_preserves_episode_id_lexical_order() -> None:
    def item(episode_id: str, rank_fingerprint: str) -> ProviderSearchItem:
        return ProviderSearchItem(
            item=MemoryItem(
                kind="episode",
                text="same text",
                provider_validated=True,
            ),
            score=1.0,
            episode_id=episode_id,
            timestamp="2026-08-26T00:00:00Z",
            provider_rank=0,
            queried_owner=PRINCIPAL,
            provider_validated=True,
            text_fingerprint="same-text-fingerprint",
            rank_fingerprint=rank_fingerprint,
        )

    ordered = sorted(
        (item("episode-z", "0000"), item("episode-a", "ffff")),
        key=memory_module._same_method_rank_key,
    )

    assert [entry.episode_id for entry in ordered] == ["episode-a", "episode-z"]


@pytest.mark.asyncio
async def test_shutdown_drops_volatile_work(tmp_path: Path) -> None:
    module, _store, provider = _module(tmp_path)
    module._writer._ensure_worker = lambda: None
    await module.capture(_request())
    await module.close_writer()
    assert provider.captures == []
    assert module._writer._queue.empty()
    assert module._writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_capture_reserves_capacity_before_slow_digest(tmp_path: Path) -> None:
    """A slow source lookup cannot grow pending captures past the writer bound."""

    module, store, _provider = _module(tmp_path)
    module._writer._permits = 1
    digest_entered = threading.Event()
    finish_digest = threading.Event()
    source_message_digest = store.source_message_digest

    def slow_digest(source_message_id: str) -> str:
        digest_entered.set()
        finish_digest.wait(timeout=1.0)
        return source_message_digest(source_message_id)

    store.source_message_digest = slow_digest
    first = asyncio.create_task(
        module.capture(_request(source_message_id="slow-digest"))
    )
    assert await asyncio.to_thread(digest_entered.wait, 1.0)
    assert module._writer._permits == 0

    second = await module.capture(_request(source_message_id="overflow"))
    assert second == CaptureSkipped(reason="memory_queue_full")
    assert module._writer._permits == 0

    finish_digest.set()
    assert await first == CaptureAccepted()
    await module.wait_writer_idle_for_tests()
    assert module._writer._permits == 1


@pytest.mark.asyncio
async def test_cancelled_pinning_releases_shared_writer_reservation(tmp_path: Path) -> None:
    module, _store, _provider = _module(tmp_path)
    pin_entered = threading.Event()
    finish_pin = threading.Event()

    class BlockingAttachmentStore:
        def __init__(self) -> None:
            self.released: list[str] = []

        def pin(self, *_args, **_kwargs):
            pin_entered.set()
            finish_pin.wait(timeout=1.0)
            return SimpleNamespace(bundle_id="cancelled-bundle")

        def release(self, bundle_id: str) -> None:
            self.released.append(bundle_id)

    attachment_store = BlockingAttachmentStore()
    module._attachment_store = attachment_store
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )
    capture = asyncio.create_task(module.capture(_request(attachments=(attachment,))))
    assert await asyncio.to_thread(pin_entered.wait, 1.0)
    assert module._writer._permits == MAX_WRITER_PERMITS - 1

    capture.cancel()
    quiescing = asyncio.create_task(module._writer.quiesce(timeout_seconds=1.0))
    await asyncio.sleep(0)
    assert not capture.done()
    assert not quiescing.done()
    finish_pin.set()

    with pytest.raises(asyncio.CancelledError):
        await capture
    assert await quiescing
    assert module._writer._permits == MAX_WRITER_PERMITS
    assert attachment_store.released == ["cancelled-bundle"]


@pytest.mark.asyncio
async def test_cancelled_pinning_cleanup_failure_disables_attachment_intake(
    tmp_path: Path,
) -> None:
    module, _store, _provider = _module(tmp_path)
    pin_entered = threading.Event()
    finish_pin = threading.Event()

    class FailingAttachmentStore:
        def pin(self, *_args, **_kwargs):
            pin_entered.set()
            finish_pin.wait(timeout=1.0)
            raise AttachmentCleanupUnprovenError(
                "memory_store_unavailable",
                "partial attachment bundle could not be reclaimed",
            )

    module._attachment_store = FailingAttachmentStore()
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )
    capture = asyncio.create_task(module.capture(_request(attachments=(attachment,))))
    assert await asyncio.to_thread(pin_entered.wait, 1.0)

    capture.cancel("caller stopped")
    finish_pin.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await capture
    assert raised.value.args == ("caller stopped",)
    assert not module._writer.attachments_enabled
    assert module._writer._permits == MAX_WRITER_PERMITS


@pytest.mark.asyncio
async def test_unadmitted_attachment_cleanup_finishes_before_permit_release(
    tmp_path: Path,
) -> None:
    module, store, _provider = _module(tmp_path)
    cleanup_entered = threading.Event()
    finish_cleanup = threading.Event()

    class BlockingAttachmentStore:
        def pin(self, *_args, **_kwargs):
            return SimpleNamespace(bundle_id="unadmitted-bundle")

        def release(self, _bundle_id: str) -> None:
            cleanup_entered.set()
            finish_cleanup.wait(timeout=1.0)

    module._attachment_store = BlockingAttachmentStore()
    store.admit_volatile_capture = lambda **_kwargs: VolatileAdmission("project_limit")
    module._writer._permits = 1
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )

    capture = asyncio.create_task(
        module.capture(_request(attachments=(attachment,)))
    )
    assert await asyncio.to_thread(cleanup_entered.wait, 1.0)
    assert module._writer._permits == 0
    assert module._writer.reserve("later") == "full"
    finish_cleanup.set()

    assert await capture == CaptureSkipped(reason="memory_invalid_input")
    assert module._writer._permits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_cancelled_unadmitted_cleanup_tracks_settled_release_result(
    tmp_path: Path,
    release_fails: bool,
) -> None:
    """MEMORY-IM-ATTACH-013: unadmitted cleanup preserves the same invariant."""

    module, _store, _provider = _module(tmp_path)
    release_entered = threading.Event()
    finish_release = threading.Event()

    class AttachmentStore:
        def release(self, _bundle_id: str) -> None:
            release_entered.set()
            finish_release.wait(timeout=1.0)
            if release_fails:
                raise OSError("cleanup failed")

    module._attachment_store = AttachmentStore()
    reservation = module._writer.reserve("cancelled-cleanup")
    assert not isinstance(reservation, str)

    cleanup = asyncio.create_task(
        module._release_unadmitted_capture(
            reservation,
            SimpleNamespace(bundle_id="bundle"),
        )
    )
    assert await asyncio.to_thread(release_entered.wait, 1.0)
    cleanup.cancel("caller stopped")
    finish_release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await cleanup

    assert raised.value.args == ("caller stopped",)
    assert not reservation.active
    assert module._writer._permits == MAX_WRITER_PERMITS
    assert module._writer.attachments_enabled is not release_fails


@pytest.mark.asyncio
async def test_unadmitted_failure_allows_same_source_retry(tmp_path: Path) -> None:
    module, store, provider = _module(tmp_path)
    admit = store.admit_volatile_capture
    attempts = 0

    def fail_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary store failure")
        return admit(**kwargs)

    store.admit_volatile_capture = fail_once

    assert await module.capture(_request()) == OperationFailed(
        error="memory_store_unavailable"
    )
    assert await module.capture(_request()) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()

    assert len(provider.captures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transition", "reason"),
    [
        ("paused", "memory_operation_in_progress"),
        ("unavailable", "memory_sidecar_unavailable"),
    ],
)
async def test_offer_preserves_writer_state_after_reservation(
    tmp_path: Path,
    transition: str,
    reason: str,
) -> None:
    module, store, _provider = _module(tmp_path)
    admit = store.admit_volatile_capture

    def transition_after_admission(**kwargs):
        admission = admit(**kwargs)
        if transition == "unavailable":
            module._writer._unavailable = True
        module._writer.pause_intake()
        return admission

    store.admit_volatile_capture = transition_after_admission

    assert await module.capture(
        _request(source_message_id=f"offer-{transition}")
    ) == CaptureSkipped(reason=reason)
    assert module._writer._permits == MAX_WRITER_PERMITS
    assert module._writer._queue.empty()
    assert store.ensure_meta().missed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    ["memory_invalid_input", "memory_input_too_large", "memory_low_disk_space"],
)
async def test_attachment_only_pin_failure_preserves_typed_skip(
    tmp_path: Path,
    error: str,
) -> None:
    module, store, _provider = _module(tmp_path)

    class FailingAttachmentStore:
        def pin(self, *_args, **_kwargs):
            raise AttachmentPinError(error, "pin failed")

    module._attachment_store = FailingAttachmentStore()
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )

    assert await module.capture(
        _request(
            source_message_id=f"attachment-{error}",
            text="",
            attachments=(attachment,),
        )
    ) == CaptureSkipped(reason=error)
    assert store.ensure_meta().missed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("project_limit", CaptureSkipped(reason="memory_invalid_input")),
        ("timestamp_invalid", CaptureSkipped(reason="memory_invalid_input")),
        ("clear_in_progress", CaptureSkipped(reason="memory_operation_in_progress")),
        ("store_unavailable", OperationFailed(error="memory_store_unavailable")),
    ],
)
async def test_caption_fallback_preserves_admission_outcome(
    tmp_path: Path,
    outcome: str,
    expected: object,
) -> None:
    module, store, _provider = _module(tmp_path)

    class FailingAttachmentStore:
        def pin(self, *_args, **_kwargs):
            raise AttachmentPinError("memory_store_unavailable", "pin failed")

    module._attachment_store = FailingAttachmentStore()
    store.admit_volatile_capture = lambda **_kwargs: VolatileAdmission(outcome)
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )

    assert await module.capture(
        _request(
            source_message_id=f"caption-fallback-{outcome}",
            text="keep the caption",
            attachments=(attachment,),
        )
    ) == expected
    assert module._writer._permits == MAX_WRITER_PERMITS
    assert module._writer._queue.empty()


@pytest.mark.asyncio
async def test_cancelled_caption_fallback_releases_reservation_for_retry(
    tmp_path: Path,
) -> None:
    """MEMORY-IM-ATTACH-013: fallback cancellation abandons volatile admission."""

    module, store, provider = _module(tmp_path)
    admission_entered = threading.Event()
    finish_admission = threading.Event()
    admit = store.admit_volatile_capture

    class FailingAttachmentStore:
        def pin(self, *_args, **_kwargs):
            raise AttachmentPinError("memory_store_unavailable", "pin failed")

    def blocking_admission(**kwargs):
        admission_entered.set()
        finish_admission.wait(timeout=1.0)
        return admit(**kwargs)

    module._attachment_store = FailingAttachmentStore()
    store.admit_volatile_capture = blocking_admission
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )
    request = _request(
        source_message_id="cancelled-caption-fallback",
        text="keep the caption",
        attachments=(attachment,),
    )

    capture = asyncio.create_task(module.capture(request))
    assert await asyncio.to_thread(admission_entered.wait, 1.0)
    capture.cancel("session changed")
    finish_admission.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await capture
    assert raised.value.args == ("session changed",)
    assert module._writer._permits == MAX_WRITER_PERMITS

    store.admit_volatile_capture = admit
    assert await module.capture(request) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()
    assert [item.text for item in provider.captures] == ["keep the caption"]


@pytest.mark.asyncio
async def test_startup_scrubs_leftover_attachment_bundles(tmp_path: Path) -> None:
    source = tmp_path / "attachments" / "avibe" / "source.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    attachment_store = AttachmentPinStore(effective_home=tmp_path)
    bundle = attachment_store.pin(
        (
            CaptureAttachment(
                kind="image",
                name="source.png",
                uri=source.as_uri(),
                ext="png",
            ),
        )
    )
    bundle_path = tmp_path / "memory" / "attachments" / bundle.relative_path
    assert bundle_path.exists()
    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )

    module = MemoryModule(
        store,
        FakeMemoryProvider(),
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        attachment_store=attachment_store,
        effective_home=tmp_path,
    )

    assert not bundle_path.exists()
    assert module._writer.attachments_enabled
    await module.close_writer()


@pytest.mark.asyncio
async def test_startup_cleanup_failure_disables_only_attachments(tmp_path: Path) -> None:
    class FailingStartupStore:
        def __init__(self) -> None:
            self.clear_calls = 0

        def clear_all(self) -> None:
            self.clear_calls += 1
            if self.clear_calls == 1:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "cleanup failed",
                )

    store = MemoryStore(
        tmp_path / "state" / "memory" / "memory.sqlite",
        effective_home=tmp_path,
    )
    provider = FakeMemoryProvider()
    module = MemoryModule(
        store,
        provider,
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        attachment_store=FailingStartupStore(),
        effective_home=tmp_path,
    )

    assert not module._writer.attachments_enabled
    assert await module.capture(_request()) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()
    assert len(provider.captures) == 1
    attachment = CaptureAttachment(
        kind="image",
        name="source.png",
        uri=(tmp_path / "attachments" / "avibe" / "source.png").as_uri(),
        ext="png",
    )
    assert await module.capture(
        _request(
            source_message_id="attachment-only",
            text="",
            attachments=(attachment,),
        )
    ) == CaptureSkipped(reason="memory_store_unavailable")
    assert await module.capture(
        _request(
            source_message_id="captioned-attachment",
            text="keep the caption",
            attachments=(attachment,),
        )
    ) == CaptureAccepted()
    await module.wait_writer_idle_for_tests()
    assert provider.captures[-1].text == "keep the caption"
    assert provider.captures[-1].attachments == ()


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
