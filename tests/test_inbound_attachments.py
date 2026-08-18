from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import core.handlers.inbound_attachments as inbound_attachment_module
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


class _DescriptorClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.target_fds: list[int] = []

    async def download_file_to_path(
        self,
        file_info,
        target_path,
        max_bytes=None,
        timeout_seconds=30,
        target_fd=None,
    ):
        assert target_fd is not None
        self.target_fds.append(target_fd)
        os.write(target_fd, self.payload)
        return FileDownloadResult(True)


class _LeaseSwapClient(_DescriptorClient):
    def __init__(self, payload: bytes, lease_root: Path, outside: Path) -> None:
        super().__init__(payload)
        self.lease_root = lease_root
        self.outside = outside

    async def download_file_to_path(self, *args, target_fd=None, **kwargs):
        lease_dir = next(self.lease_root.iterdir())
        moved = self.lease_root / f"{lease_dir.name}.moved"
        lease_dir.rename(moved)
        lease_dir.symlink_to(self.outside, target_is_directory=True)
        return await super().download_file_to_path(
            *args,
            target_fd=target_fd,
            **kwargs,
        )


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
@pytest.mark.parametrize("owned_component", ["attachments", "im"])
async def test_materializer_rejects_symlinked_attachment_root(
    tmp_path: Path,
    owned_component: str,
) -> None:
    """Every component Avibe owns below the home stays a no-follow boundary."""

    home = tmp_path / "avibe-home"
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = home / "attachments" if owned_component == "attachments" else home / "attachments" / "im"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(outside, target_is_directory=True)
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
async def test_materializer_traverses_a_symlinked_parent_above_the_home(
    tmp_path: Path,
) -> None:
    """A home reached through symlinked parents is a layout, not an escape.

    Installs where ``/home/<user>`` points at another volume, and the legacy
    ``~/.vibe_remote`` back-symlink, both reach the Avibe home through a
    symlink the operator owns. Linux answers ``O_NOFOLLOW | O_DIRECTORY`` on a
    symlink with ``ENOTDIR``, so a walk that starts at ``/`` used to fail on
    the symlinked component before reaching anything Avibe owns.
    """

    physical = tmp_path / "volume" / "user"
    physical.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "volume", target_is_directory=True)
    home = tmp_path / "home" / "user" / ".avibe"
    client = _StubClient({"notes.txt": b"notes"})
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref", size=5)],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
    )

    assert batch.errors == ()
    path = Path(batch.attachments[0].local_path or "")
    assert path.read_bytes() == b"notes"
    assert path.is_relative_to(physical / ".avibe" / "attachments" / "im")
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_traverses_a_symlinked_parent_for_a_declared_root(
    tmp_path: Path,
) -> None:
    """The declared ``attachments_root`` carries the same anchor rule."""

    physical = tmp_path / "volume" / "user"
    physical.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "volume", target_is_directory=True)
    attachments_root = tmp_path / "home" / "user" / ".avibe" / "attachments"
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref", size=5)],
    )

    batch = await InboundAttachmentMaterializer(
        attachments_root=attachments_root,
    ).materialize(context, _StubClient({"notes.txt": b"notes"}))

    assert batch.errors == ()
    path = Path(batch.attachments[0].local_path or "")
    assert path.is_relative_to(physical / ".avibe" / "attachments" / "im")
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_keeps_download_writes_on_anchored_descriptor(
    tmp_path: Path,
) -> None:
    home = tmp_path / "avibe-home"
    client = _DescriptorClient(b"anchored")
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref")],
    )

    batch = await InboundAttachmentMaterializer(effective_home=home).materialize(
        context,
        client,
    )

    assert len(client.target_fds) == 1
    assert Path(batch.attachments[0].local_path or "").read_bytes() == b"anchored"
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_fails_closed_when_lease_directory_is_replaced(
    tmp_path: Path,
) -> None:
    home = tmp_path / "avibe-home"
    lease_root = home / "attachments" / "im"
    outside = tmp_path / "outside"
    outside.mkdir()
    client = _LeaseSwapClient(b"anchored", lease_root, outside)
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="slack",
        files=[FileAttachment("notes.txt", "text/plain", url="ref")],
    )

    with pytest.raises(OSError, match="lease directory changed"):
        await InboundAttachmentMaterializer(effective_home=home).materialize(
            context,
            client,
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_materializer_rejects_declared_oversize_before_adapter(
    tmp_path: Path,
) -> None:
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="telegram",
        files=[FileAttachment("large.pdf", "application/pdf", url="ref", size=20)],
    )

    client = _StubClient({"large.pdf": b"small"})
    batch = await InboundAttachmentMaterializer(effective_home=tmp_path / "home").materialize(
        context,
        client,
        max_bytes=10,
        language="zh",
    )

    assert client.calls == []
    assert batch.errors == ("file_too_large",)
    assert batch.display_errors == ("附件“large.pdf”超过附件大小限制。",)
    batch.lease.release()


@pytest.mark.asyncio
async def test_materializer_preserves_adapter_typed_size_reason(
    tmp_path: Path,
) -> None:
    context = MessageContext(
        user_id="U1",
        channel_id="D1",
        platform="telegram",
        files=[FileAttachment("large.pdf", "application/pdf", url="ref")],
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
    original_fchmod = inbound_attachment_module.os.fchmod
    private_file_calls = 0

    def fail_corrected_chmod(descriptor: int, mode: int) -> None:
        nonlocal private_file_calls
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            private_file_calls += 1
        if private_file_calls == 2:
            raise OSError("post-download chmod failed")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(inbound_attachment_module.os, "fchmod", fail_corrected_chmod)

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
