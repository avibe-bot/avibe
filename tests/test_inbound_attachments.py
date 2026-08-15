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
