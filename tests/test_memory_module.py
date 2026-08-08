from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from config import paths
from core.memory.attachments import AttachmentPinStore
from core.memory.everos import FakeMemoryProvider, MemoryProviderFailure
from core.memory.module import (
    MAX_CAPTURE_ATTACHMENT_METADATA_BYTES,
    MAX_CAPTURE_IDENTIFIER_BYTES,
    MAX_CAPTURE_TEXT_BYTES,
    MAX_QUERY_BYTES,
    MIN_FREE_DISK_BYTES,
    MemoryModule,
)
from core.memory.store import MemoryStore
from core.memory.types import (
    CLOSED_MEMORY_ERROR_CODES,
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureSkipped,
    MemoryItem,
    MemoryItems,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    OperationFailed,
    RecallItems,
    RecallPolicy,
)


PROJECT = "p-22222222222222222222222222222222"
PRINCIPAL = "u-11111111111111111111111111111111"
ROOT_SENTINEL_FILENAME = ".avibe-memory-root.json"


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
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    sentinel = root / ROOT_SENTINEL_FILENAME
    sentinel.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider_root_id": meta.provider_root_id,
                "provider_id": "everos",
                "provider_root_format": "slice1",
                "created_by_artifact_fingerprint": "slice1-core",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    sentinel.chmod(0o600)


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

    assert await module.capture(_request(attachments=(attachment,))) == CaptureAccepted()
    queued = store.list_queue_rows()[0]
    assert queued.payload_attachments is not None
    assert queued.attachment_bundle_id is not None
    assert attachment.uri not in queued.payload_attachments

    source_path.unlink()
    assert await module._worker.drain_once() == 1
    forwarded = provider.captures[0].attachments[0]
    assert forwarded.name == attachment.name
    assert forwarded.uri != attachment.uri
    assert store.list_queue_rows()[0].payload_attachments is None


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


async def test_agentic_recall_fails_closed_without_provider_search(tmp_path: Path) -> None:
    provider = FakeMemoryProvider()
    module, _store, _provider = _module(tmp_path, provider=provider)
    policy = RecallPolicy(
        mode="agentic",
        max_results=4,
        timeout_seconds=5,
        max_model_calls=1,
        cost_budget_tokens=1_000,
    )

    result = await module.recall(
        "query",
        policy=policy,
        principal_id=PRINCIPAL,
        project_id=PROJECT,
    )

    assert result == OperationFailed(error="memory_capability_unavailable")
    assert provider.search_scopes == []
    assert provider.search_policies == []


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


def test_empty_provider_root_format_activation_allows_generated_control_files(tmp_path: Path) -> None:
    provider_root = paths.get_vibe_remote_dir() / "memory" / "provider-root"
    module, store, _provider = _module(
        tmp_path,
        owned_provider_root=True,
        provider_root=provider_root,
    )
    (provider_root / "everos.toml").write_text("[memory]\n", encoding="utf-8")
    (provider_root / "ome.toml").write_text("[strategies]\n", encoding="utf-8")
    module._set_runtime_artifact_metadata(
        provider_root_format="everos-2.0",
        artifact_fingerprint="artifact-2.0",
        compatible_provider_root_formats=(),
    )

    assert module._activate_empty_provider_root_format(store.ensure_meta()) is True
    sentinel = json.loads((provider_root / ROOT_SENTINEL_FILENAME).read_text(encoding="utf-8"))
    assert sentinel["provider_root_format"] == "everos-2.0"
    assert sentinel["created_by_artifact_fingerprint"] == "artifact-2.0"


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


def test_slice2_runtime_types_remain_internal_to_the_memory_package() -> None:
    import core.memory as memory
    import core.memory.artifact as artifact
    import core.memory.process as process

    assert hasattr(artifact, "MemoryArtifactManager")
    assert hasattr(process, "EverOSProcess")
    assert "MemoryArtifactManager" not in memory.__all__
    assert "EverOSProcess" not in memory.__all__
