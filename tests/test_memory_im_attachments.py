from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

import core.memory.im_attachments as im_attachment_module
from core.handlers.inbound_attachments import InboundAttachmentMaterializer
from core.memory.attachments import AttachmentPinStore
from core.memory.im_attachments import select_memory_attachments
from core.memory.types import CaptureAttachment
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext


class _Client:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    async def download_file_to_path(self, file_info, target_path, max_bytes=None, timeout_seconds=30):
        Path(target_path).write_bytes(self.payloads[file_info["name"]])
        return FileDownloadResult(True)


async def _materialize(
    home: Path,
    entries: list[tuple[str, object, bytes, int | None]],
):
    files = [
        FileAttachment(name=name, mimetype=mime, url=name, size=declared)
        for name, mime, _payload, declared in entries
    ]
    payloads = {name: payload for name, _mime, payload, _declared in entries}
    return await InboundAttachmentMaterializer(effective_home=home).materialize(
        MessageContext(
            user_id="U1",
            channel_id="D1",
            platform="slack",
            files=files,
        ),
        _Client(payloads),
    )


def _xlsx_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", b"content types")
        archive.writestr("xl/workbook.xml", b"workbook")
    return payload.getvalue()


@pytest.mark.asyncio
async def test_memory_selection_normalizes_missing_mime_without_losing_siblings(
    tmp_path: Path,
) -> None:
    batch = await _materialize(
        tmp_path / "avibe-home",
        [
            ("unknown.txt", None, b"text", 4),
            ("valid.pdf", "application/pdf", b"%PDF-1.7\n", 9),
        ],
    )

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == [
        "unknown.txt",
        "valid.pdf",
    ]
    assert selected.skipped == ()
    batch.lease.release()


@pytest.mark.asyncio
async def test_memory_im_attach_004_invalid_items_preserve_valid_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEMORY-IM-ATTACH-004: per-item limits/type failures degrade independently."""

    home = tmp_path / "avibe-home"
    batch = await _materialize(
        home,
        [
            ("valid.pdf", "application/pdf", b"%PDF-1.7\nvalid", 14),
            ("video.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42", 12),
            ("oversized.txt", "text/plain", b"12345", 20),
        ],
    )
    monkeypatch.setattr(im_attachment_module, "MAX_PINNED_ATTACHMENT_BYTES", 16)

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == ["valid.pdf"]
    assert selected.skipped == ("unsupported_type", "file_too_large")
    store = AttachmentPinStore(effective_home=home)
    bundle = store.pin(selected.attachments, source_lease=batch.lease)
    assert bundle.attachments[0].name == "valid.pdf"
    store.release(bundle.bundle_id)
    batch.lease.release()
    assert list((home / "attachments" / "im").iterdir()) == []


@pytest.mark.asyncio
async def test_memory_selection_checks_magic_and_bundle_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "avibe-home"
    batch = await _materialize(
        home,
        [
            ("fake.png", "image/png", b"not a png", 9),
            ("first.txt", "text/plain", b"123", 3),
            ("second.txt", "text/plain", b"456", 3),
        ],
    )
    monkeypatch.setattr(im_attachment_module, "MAX_PINNED_BUNDLE_BYTES", 4)

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == ["first.txt"]
    assert selected.skipped == ("unsupported_type", "bundle_too_large")
    batch.lease.release()


@pytest.mark.asyncio
async def test_memory_selection_enforces_eight_file_limit(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    batch = await _materialize(
        home,
        [(f"item-{index}.txt", "text/plain", b"x", 1) for index in range(9)],
    )

    selected = select_memory_attachments(batch.lease)

    assert len(selected.attachments) == 8
    assert selected.skipped == ("count_limit",)
    batch.lease.release()


@pytest.mark.asyncio
async def test_memory_selection_counts_only_supported_survivors(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    entries = [
        (f"video-{index}.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42", 12)
        for index in range(8)
    ]
    entries.append(("valid.pdf", "application/pdf", b"%PDF-1.7\n", 9))
    batch = await _materialize(home, entries)

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == ["valid.pdf"]
    assert selected.skipped == ("unsupported_type",) * 8
    batch.lease.release()


@pytest.mark.asyncio
async def test_memory_selection_skips_office_without_soffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: False)
    batch = await _materialize(
        tmp_path / "avibe-home",
        [
            ("valid.pdf", "application/pdf", b"%PDF-1.7\n", 9),
            (
                "report.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                _xlsx_bytes(),
                None,
            ),
        ],
    )

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == ["valid.pdf"]
    assert selected.skipped == ("unsupported_type",)
    batch.lease.release()


@pytest.mark.asyncio
async def test_memory_selection_admits_office_when_soffice_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.memory.modality.office_conversion_available", lambda: True)
    batch = await _materialize(
        tmp_path / "avibe-home",
        [
            (
                "report.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                _xlsx_bytes(),
                None,
            ),
            ("valid.pdf", "application/pdf", b"%PDF-1.7\n", 9),
        ],
    )

    selected = select_memory_attachments(batch.lease)

    assert [item.name for item in selected.attachments] == ["report.xlsx", "valid.pdf"]
    assert [item.kind for item in selected.attachments] == ["doc", "pdf"]
    assert selected.skipped == ()
    batch.lease.release()


@pytest.mark.asyncio
async def test_long_materialized_filename_preserves_supported_extension(
    tmp_path: Path,
) -> None:
    home = tmp_path / "avibe-home"
    long_name = f"{'report' * 40}.pdf"
    batch = await _materialize(
        home,
        [(long_name, "application/pdf", b"%PDF-1.7\n", 9)],
    )

    selected = select_memory_attachments(batch.lease)

    assert len(selected.attachments) == 1
    assert selected.attachments[0].name.endswith(".pdf")
    assert len(selected.attachments[0].name.encode("utf-8")) <= 200
    batch.lease.release()


@pytest.mark.asyncio
async def test_pin_rejects_released_or_mismatched_im_lease(tmp_path: Path) -> None:
    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    batch = await _materialize(
        first_home,
        [("notes.txt", "text/plain", b"notes", 5)],
    )
    selected = select_memory_attachments(batch.lease)

    with pytest.raises(Exception) as mismatched:
        AttachmentPinStore(effective_home=second_home).pin(
            selected.attachments,
            source_lease=batch.lease,
        )
    assert getattr(mismatched.value, "error", None) == "memory_invalid_input"

    original = selected.attachments[0]
    extra = Path(original.uri.removeprefix("file://")).parent / "extra.txt"
    extra.write_text("extra", encoding="utf-8")
    extra.chmod(0o600)
    forged = CaptureAttachment(
        kind="doc",
        name="extra.txt",
        uri=extra.as_uri(),
        ext="txt",
    )
    with pytest.raises(Exception) as unleased:
        AttachmentPinStore(effective_home=first_home).pin(
            (forged,),
            source_lease=batch.lease,
        )
    assert getattr(unleased.value, "error", None) == "memory_invalid_input"

    batch.lease.release()
    with pytest.raises(Exception) as released:
        AttachmentPinStore(effective_home=first_home).pin(
            selected.attachments,
            source_lease=batch.lease,
        )
    assert getattr(released.value, "error", None) == "memory_invalid_input"


@pytest.mark.asyncio
async def test_pin_reads_original_inode_after_lease_entry_is_replaced(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    batch = await _materialize(
        home,
        [("notes.txt", "text/plain", b"original", 8)],
    )
    selected = select_memory_attachments(batch.lease)
    source = Path(selected.attachments[0].uri.removeprefix("file://"))
    lease_dir = source.parent
    moved = lease_dir.with_name(f"{lease_dir.name}.moved")
    lease_dir.rename(moved)
    lease_dir.mkdir(mode=0o700)
    replacement = lease_dir / source.name
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o600)

    selected_after_replacement = select_memory_attachments(batch.lease)
    assert [item.name for item in selected_after_replacement.attachments] == ["notes.txt"]

    store = AttachmentPinStore(effective_home=home)
    bundle = store.pin(selected_after_replacement.attachments, source_lease=batch.lease)
    pinned = home / "memory" / "attachments" / bundle.attachments[0].storage_key

    assert pinned.read_bytes() == b"original"

    store.release(bundle.bundle_id)
    os.unlink(replacement)
    os.rmdir(lease_dir)
    moved.rename(lease_dir)
    batch.lease.release()


@pytest.mark.asyncio
async def test_pin_rejects_same_inode_same_size_content_replacement(tmp_path: Path) -> None:
    home = tmp_path / "avibe-home"
    batch = await _materialize(
        home,
        [("notes.txt", "text/plain", b"original", 8)],
    )
    selected = select_memory_attachments(batch.lease)
    source = Path(selected.attachments[0].uri.removeprefix("file://"))
    original_inode = source.stat().st_ino
    source.write_bytes(b"tampered")
    source.chmod(0o600)
    assert source.stat().st_ino == original_inode

    with pytest.raises(Exception) as changed:
        AttachmentPinStore(effective_home=home).pin(
            selected.attachments,
            source_lease=batch.lease,
        )

    assert getattr(changed.value, "error", None) == "memory_invalid_input"
    batch.lease.release()
