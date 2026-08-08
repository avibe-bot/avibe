from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import core.memory.attachments as attachment_module
from core.memory.attachments import (
    AttachmentPinError,
    AttachmentPinStore,
    PinnedAttachment,
    PinnedBundle,
    workbench_capture_attachments,
)
from core.memory.types import CaptureAttachment


@pytest.fixture
def attachment_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "avibe-home"
    monkeypatch.setenv("AVIBE_HOME", str(home))
    source_root = home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source_root.chmod(0o700)
    return home, source_root


def _source_file(source_root: Path, name: str, payload: bytes = b"attachment payload") -> Path:
    path = source_root / name
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = path.parent
    while current != source_root:
        current.chmod(0o700)
        current = current.parent
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _attachment(path: Path, *, name: str | None = None) -> CaptureAttachment:
    extension = path.suffix.lstrip(".").lower()
    return CaptureAttachment(
        kind="pdf" if extension == "pdf" else "doc",
        name=name or path.name,
        uri=path.as_uri(),
        ext=extension,
    )


def _assert_pin_error(error: pytest.ExceptionInfo[AttachmentPinError], expected: str) -> None:
    assert error.value.error == expected


def test_pin_is_private_relative_durable_and_releasable(attachment_roots) -> None:
    home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt", b"original bytes")
    store = AttachmentPinStore()

    bundle = store.pin((_attachment(source),))

    assert set(field.name for field in fields(PinnedAttachment)) == {
        "kind",
        "name",
        "ext",
        "storage_key",
        "size_bytes",
        "sha256",
    }
    assert bundle.total_bytes == len(b"original bytes")
    assert bundle.relative_path == f"bundles/{bundle.bundle_id}"
    assert bundle.attachments[0].storage_key == f"bundles/{bundle.bundle_id}/00.txt"
    assert bundle.attachments[0].sha256 == hashlib.sha256(b"original bytes").hexdigest()
    assert not Path(bundle.attachments[0].storage_key).is_absolute()

    root = home / "memory" / "attachments"
    pinned_path = root / bundle.attachments[0].storage_key
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "staging").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "bundles").stat().st_mode) == 0o700
    assert stat.S_IMODE(pinned_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(pinned_path.stat().st_mode) == 0o600

    source.unlink()
    projected = store.provider_attachments(bundle)
    assert len(projected) == 1
    assert Path(projected[0].uri.removeprefix("file://")).read_bytes() == b"original bytes"
    assert projected[0].name == "notes.txt"

    store.release(bundle.bundle_id)
    store.release(bundle.bundle_id)
    assert not pinned_path.parent.exists()


def test_workbench_conversion_preserves_symlink_for_pin_rejection(attachment_roots) -> None:
    _home, source_root = attachment_roots
    target = _source_file(source_root, "real.txt")
    link = source_root / "linked.txt"
    link.symlink_to(target)
    converted = workbench_capture_attachments(
        [SimpleNamespace(name="linked.txt", mimetype="text/plain", local_path=str(link))]
    )

    assert converted[0].uri == link.as_uri()
    with pytest.raises(AttachmentPinError) as error:
        AttachmentPinStore().pin(converted)
    _assert_pin_error(error, "memory_invalid_input")


@pytest.mark.parametrize(
    "uri_factory",
    [
        lambda path: f"https://example.test/{path.name}",
        lambda path: f"file:{path.name}",
        lambda path: f"file://remote{path}",
        lambda path: f"{path.as_uri()}?download=1",
        lambda path: f"{path.as_uri()}#fragment",
        lambda path: path.as_uri().replace("notes.txt", "%ZZ.txt"),
        lambda path: path.parent.as_uri() + "/%2e%2e/outside.txt",
        lambda path: path.parent.as_uri() + "/bad%00name.txt",
    ],
)
def test_pin_rejects_noncanonical_file_uris(attachment_roots, uri_factory) -> None:
    _home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt")
    attachment = _attachment(source)
    attachment = CaptureAttachment(
        kind=attachment.kind,
        name=attachment.name,
        uri=uri_factory(source),
        ext=attachment.ext,
    )

    with pytest.raises(AttachmentPinError) as error:
        AttachmentPinStore().pin((attachment,))

    _assert_pin_error(error, "memory_invalid_input")


def test_pin_accepts_localhost_file_uri(attachment_roots) -> None:
    _home, source_root = attachment_roots
    source = _source_file(source_root, "space name.txt")
    attachment = _attachment(source)
    attachment = CaptureAttachment(
        kind=attachment.kind,
        name=attachment.name,
        uri=f"file://localhost{quote(str(source))}",
        ext=attachment.ext,
    )

    bundle = AttachmentPinStore().pin((attachment,))

    assert bundle.attachments[0].size_bytes == source.stat().st_size


def test_pin_rejects_outside_symlink_parent_and_nonregular_sources(attachment_roots) -> None:
    home, source_root = attachment_roots
    outside = _source_file(home, "outside.txt")
    store = AttachmentPinStore()

    with pytest.raises(AttachmentPinError) as outside_error:
        store.pin((_attachment(outside),))
    _assert_pin_error(outside_error, "memory_invalid_input")

    real_parent = source_root / "real-parent"
    nested = _source_file(real_parent, "nested.txt")
    linked_parent = source_root / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(AttachmentPinError) as symlink_error:
        store.pin((_attachment(linked_parent / nested.name),))
    _assert_pin_error(symlink_error, "memory_invalid_input")

    if hasattr(os, "mkfifo"):
        fifo = source_root / "pipe.txt"
        os.mkfifo(fifo, mode=0o600)
        with pytest.raises(AttachmentPinError) as fifo_error:
            store.pin((_attachment(fifo),))
        _assert_pin_error(fifo_error, "memory_invalid_input")


@pytest.mark.parametrize("unsafe_target", ["parent", "file", "root"])
def test_pin_rejects_group_or_world_writable_sources(attachment_roots, unsafe_target) -> None:
    _home, source_root = attachment_roots
    source = _source_file(source_root, "nested/notes.txt")
    if unsafe_target == "parent":
        source.parent.chmod(0o720)
    elif unsafe_target == "file":
        source.chmod(0o620)
    else:
        source_root.chmod(0o702)

    with pytest.raises(AttachmentPinError) as error:
        AttachmentPinStore().pin((_attachment(source),))

    _assert_pin_error(error, "memory_invalid_input")


def test_pin_rejects_unowned_source(attachment_roots, monkeypatch: pytest.MonkeyPatch) -> None:
    _home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt")
    store = AttachmentPinStore()
    real_uid = os.getuid()
    calls = 0

    def uid_for_layout_then_source() -> int:
        nonlocal calls
        calls += 1
        # Pin verifies the three durable roots, reopens staging/bundles, then
        # creates and reopens the staging bundle before it reaches the source.
        return real_uid + 1 if calls == 8 else real_uid

    monkeypatch.setattr(attachment_module.os, "getuid", uid_for_layout_then_source)
    with pytest.raises(AttachmentPinError) as error:
        store.pin((_attachment(source),))

    _assert_pin_error(error, "memory_invalid_input")


def test_pin_enforces_count_file_and_total_limits(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    first = _source_file(source_root, "first.txt", b"1234")
    second = _source_file(source_root, "second.txt", b"567")
    store = AttachmentPinStore()

    with pytest.raises(AttachmentPinError) as count_error:
        store.pin(tuple(_attachment(first) for _ in range(9)))
    _assert_pin_error(count_error, "memory_input_too_large")

    monkeypatch.setattr(attachment_module, "MAX_PINNED_ATTACHMENT_BYTES", 3)
    with pytest.raises(AttachmentPinError) as file_error:
        store.pin((_attachment(first),))
    _assert_pin_error(file_error, "memory_input_too_large")

    monkeypatch.setattr(attachment_module, "MAX_PINNED_ATTACHMENT_BYTES", 4)
    monkeypatch.setattr(attachment_module, "MAX_PINNED_BUNDLE_BYTES", 6)
    with pytest.raises(AttachmentPinError) as total_error:
        store.pin((_attachment(first), _attachment(second)))
    _assert_pin_error(total_error, "memory_input_too_large")


def test_pin_detects_source_mutation_and_removes_staging(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt", b"before")
    store = AttachmentPinStore()
    original_read = attachment_module.os.read
    changed = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if chunk and not changed:
            changed = True
            source.write_bytes(b"after!")
            source.chmod(0o600)
        return chunk

    monkeypatch.setattr(attachment_module.os, "read", mutate_after_first_read)
    with pytest.raises(AttachmentPinError) as error:
        store.pin((_attachment(source),))

    _assert_pin_error(error, "memory_invalid_input")
    root = home / "memory" / "attachments"
    assert list((root / "staging").iterdir()) == []
    assert list((root / "bundles").iterdir()) == []


def test_pin_fails_closed_on_fsync_and_maps_disk_full(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt")
    store = AttachmentPinStore()
    original_fsync = attachment_module.os.fsync
    calls = 0

    def fail_file_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "full")
        original_fsync(descriptor)

    monkeypatch.setattr(attachment_module.os, "fsync", fail_file_fsync)
    with pytest.raises(AttachmentPinError) as error:
        store.pin((_attachment(source),))

    _assert_pin_error(error, "memory_low_disk_space")
    root = home / "memory" / "attachments"
    assert list((root / "staging").iterdir()) == []
    assert list((root / "bundles").iterdir()) == []


def test_pin_renames_atomically_before_final_parent_fsync(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt")
    store = AttachmentPinStore()
    original_rename = attachment_module.os.rename
    original_fsync = attachment_module.os.fsync
    events: list[tuple[str, bool]] = []

    def record_rename(*args, **kwargs) -> None:
        events.append(("rename", kwargs.get("src_dir_fd") is not None and kwargs.get("dst_dir_fd") is not None))
        original_rename(*args, **kwargs)

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", stat.S_ISDIR(os.fstat(descriptor).st_mode)))
        original_fsync(descriptor)

    monkeypatch.setattr(attachment_module.os, "rename", record_rename)
    monkeypatch.setattr(attachment_module.os, "fsync", record_fsync)
    store.pin((_attachment(source),))

    rename_index = next(index for index, event in enumerate(events) if event[0] == "rename")
    assert events[rename_index] == ("rename", True)
    assert [event for event in events[rename_index + 1 :] if event == ("fsync", True)] == [
        ("fsync", True),
        ("fsync", True),
    ]


def test_provider_projection_rejects_tampered_pinned_bytes(attachment_roots) -> None:
    home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt", b"original")
    store = AttachmentPinStore()
    bundle = store.pin((_attachment(source),))
    pinned_path = home / "memory" / "attachments" / bundle.attachments[0].storage_key
    pinned_path.write_bytes(b"tampered")
    pinned_path.chmod(0o600)

    with pytest.raises(AttachmentPinError) as error:
        store.provider_attachments(bundle)

    _assert_pin_error(error, "memory_store_unavailable")


def test_provider_projection_rejects_untracked_bundle_file(attachment_roots) -> None:
    home, source_root = attachment_roots
    source = _source_file(source_root, "notes.txt")
    store = AttachmentPinStore()
    bundle = store.pin((_attachment(source),))
    bundle_path = home / "memory" / "attachments" / bundle.relative_path
    extra = bundle_path / "01.txt"
    extra.write_bytes(b"untracked")
    extra.chmod(0o600)

    with pytest.raises(AttachmentPinError) as error:
        store.provider_attachments(bundle)

    _assert_pin_error(error, "memory_store_unavailable")


def test_reconcile_preserves_references_and_removes_releasing_orphans_and_staging(
    attachment_roots,
) -> None:
    home, source_root = attachment_roots
    store = AttachmentPinStore()
    referenced = store.pin((_attachment(_source_file(source_root, "referenced.txt")),))
    releasing = store.pin((_attachment(_source_file(source_root, "releasing.txt")),))
    orphan = store.pin((_attachment(_source_file(source_root, "orphan.txt")),))

    root = home / "memory" / "attachments"
    stale_id = "f" * 32
    stale = root / "staging" / f"{stale_id}.tmp"
    stale.mkdir(mode=0o700)
    partial = stale / "00.txt"
    partial.write_bytes(b"partial")
    partial.chmod(0o000)
    unknown = root / "staging" / "keep.txt"
    unknown.write_text("keep", encoding="utf-8")

    removed = store.reconcile({referenced.bundle_id}, {releasing.bundle_id})

    assert removed == tuple(sorted((orphan.bundle_id, releasing.bundle_id)))
    assert (root / referenced.relative_path).is_dir()
    assert not (root / releasing.relative_path).exists()
    assert not (root / orphan.relative_path).exists()
    assert not stale.exists()
    assert unknown.read_text(encoding="utf-8") == "keep"
    assert store.reconcile({referenced.bundle_id}, {releasing.bundle_id}) == ()


def test_reconcile_reports_missing_reference(attachment_roots) -> None:
    _home, _source_root = attachment_roots
    store = AttachmentPinStore()

    with pytest.raises(AttachmentPinError) as error:
        store.reconcile({"a" * 32}, set())

    _assert_pin_error(error, "memory_store_unavailable")


def test_release_never_follows_bundle_symlink(attachment_roots) -> None:
    home, _source_root = attachment_roots
    store = AttachmentPinStore()
    outside = home / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    bundle_id = "b" * 32
    link = home / "memory" / "attachments" / "bundles" / bundle_id
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AttachmentPinError) as error:
        store.release(bundle_id)

    _assert_pin_error(error, "memory_store_unavailable")
    assert link.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_store_rejects_symlinked_storage_parent(attachment_roots) -> None:
    home, _source_root = attachment_roots
    outside = home / "outside-memory"
    outside.mkdir(mode=0o700)
    memory = home / "memory"
    memory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AttachmentPinError) as error:
        AttachmentPinStore()

    _assert_pin_error(error, "memory_store_unavailable")
    assert list(outside.iterdir()) == []


def test_persisted_bundle_rejects_absolute_or_cross_bundle_storage_keys() -> None:
    digest = hashlib.sha256(b"x").hexdigest()
    bundle_id = "a" * 32
    with pytest.raises(ValueError, match="storage key"):
        PinnedAttachment(
            kind="doc",
            name="x.txt",
            ext="txt",
            storage_key="/tmp/x.txt",
            size_bytes=1,
            sha256=digest,
        )
    with pytest.raises(ValueError, match="storage key"):
        PinnedBundle(
            bundle_id=bundle_id,
            attachments=(
                PinnedAttachment(
                    kind="doc",
                    name="x.txt",
                    ext="txt",
                    storage_key=f"bundles/{'b' * 32}/00.txt",
                    size_bytes=1,
                    sha256=digest,
                ),
            ),
            total_bytes=1,
        )
