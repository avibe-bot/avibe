from __future__ import annotations

from pathlib import Path

import pytest

from core.handlers.inbound_attachments import InboundAttachmentMaterializer
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext


class _StubClient:
    def __init__(self, payloads: dict[str, bytes | None]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, int | None, int]] = []

    async def download_file_to_path(
        self,
        file_info,
        target_path,
        max_bytes=None,
        timeout_seconds=30,
    ):
        self.calls.append((file_info["name"], max_bytes, timeout_seconds))
        payload = self.payloads[file_info["name"]]
        if payload is None:
            Path(target_path).write_bytes(b"partial")
            return FileDownloadResult(False, "native secret must not escape")
        Path(target_path).write_bytes(payload)
        return FileDownloadResult(True)


class _LegacyPathClient:
    def __init__(self) -> None:
        self.calls = 0

    async def download_file_to_path(self, file_info, target_path):
        self.calls += 1
        Path(target_path).write_bytes(b"legacy")
        return FileDownloadResult(True)


class _OversizedClient:
    async def download_file_to_path(
        self,
        file_info,
        target_path,
        max_bytes=None,
        timeout_seconds=30,
    ):
        return FileDownloadResult(False, "native size detail", "file_too_large")


@pytest.mark.asyncio
async def test_materializer_publishes_one_private_reference_counted_lease(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    client = _StubClient({"../report.pdf": b"%PDF-1.7\n"})
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[
            FileAttachment(
                name="../report.pdf",
                mimetype="application/pdf",
                url="private-native-reference",
                size=9,
            )
        ],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
    )

    assert batch.errors == ()
    assert client.calls == [("../report.pdf", None, 30)]
    assert len(batch.attachments) == 1
    attachment = batch.attachments[0]
    assert attachment.name == "report.pdf"
    assert attachment.local_path is not None
    path = Path(attachment.local_path)
    assert path.read_bytes() == b"%PDF-1.7\n"
    assert path.is_relative_to(home / "attachments" / "im")
    assert path.stat().st_mode & 0o077 == 0

    retained = batch.lease.retain()
    batch.lease.release()
    assert path.exists()
    retained.release()
    assert not path.exists()


@pytest.mark.asyncio
async def test_materializer_degrades_failed_sibling_and_removes_partial_file(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    client = _StubClient({"valid.txt": b"valid", "failed.txt": None})
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[
            FileAttachment("valid.txt", "text/plain", url="valid", size=5),
            FileAttachment("failed.txt", "text/plain", url="failed", size=10),
        ],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
        max_concurrency=2,
    )

    assert [item.name for item in batch.attachments] == ["valid.txt"]
    assert batch.errors == ("download_failed",)
    lease_root = home / "attachments" / "im"
    assert not list(lease_root.rglob("*.part"))
    assert "native secret" not in repr(batch)
    batch.lease.release()
    assert list(lease_root.iterdir()) == []


@pytest.mark.asyncio
async def test_adoption_does_not_preserve_an_empty_failed_batch(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    client = _StubClient({"failed.txt": None})
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("failed.txt", "text/plain", url="failed", size=10)],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(context, client)
    batch.lease.adopt()
    batch.lease.release()

    assert list((home / "attachments" / "im").iterdir()) == []


@pytest.mark.asyncio
async def test_materializer_adoption_preserves_agent_owned_files(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    client = _StubClient({"notes.txt": b"notes"})
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref", size=5)],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(context, client)
    path = Path(batch.attachments[0].local_path or "")
    batch.lease.adopt()
    batch.lease.release()

    assert path.read_bytes() == b"notes"


@pytest.mark.asyncio
async def test_materializer_preserves_legacy_two_argument_path_clients(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    client = _LegacyPathClient()
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("legacy.txt", "text/plain", url="ref", size=6)],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
        max_bytes=10,
        timeout_seconds=7,
    )

    assert client.calls == 1
    assert batch.attachments[0].size == 6
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_rejects_symlinked_attachment_root(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    attachments = home / "attachments"
    outside = tmp_path / "outside"
    attachments.mkdir(parents=True)
    outside.mkdir()
    (attachments / "im").symlink_to(outside, target_is_directory=True)
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref", size=5)],
    )

    with pytest.raises(OSError):
        await InboundAttachmentMaterializer(effective_home=home).materialize(
            context,
            _StubClient({"notes.txt": b"notes"}),
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_materializer_preserves_typed_size_reason_and_localizes_display(
    tmp_path: Path,
) -> None:
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="telegram",
        files=[FileAttachment("large.pdf", "application/pdf", url="ref", size=20)],
    )

    batch = await InboundAttachmentMaterializer(effective_home=tmp_path / "home").materialize(
        context,
        _OversizedClient(),
        max_bytes=10,
        language="zh",
    )

    assert batch.errors == ("file_too_large",)
    assert batch.display_errors == ("附件“large.pdf”超过附件大小限制。",)
    assert "native size detail" not in repr(batch)
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_removes_corrected_final_file_when_post_processing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "avibe-home"
    client = _StubClient(
        {
            "failed.bin": b"\x89PNG\r\n\x1a\ncontent",
            "valid.txt": b"valid",
        }
    )
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[
            FileAttachment("failed.bin", "application/octet-stream", url="failed"),
            FileAttachment("valid.txt", "text/plain", url="valid"),
        ],
    )
    original_chmod = Path.chmod

    def fail_corrected_chmod(path: Path, mode: int) -> None:
        if path.suffix == ".png":
            raise OSError("post-download chmod failed")
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_corrected_chmod)

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
        max_concurrency=2,
    )

    assert [attachment.name for attachment in batch.attachments] == ["valid.txt"]
    assert batch.errors == ("download_failed",)
    lease_root = home / "attachments" / "im"
    assert not list(lease_root.rglob("*failed*"))
    batch.lease.adopt()
    batch.lease.release()
    assert not list(lease_root.rglob("*failed*"))
