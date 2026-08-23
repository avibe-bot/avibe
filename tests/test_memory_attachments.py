from __future__ import annotations

import errno
import hashlib
import io
import os
import sqlite3
import stat
import zipfile
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import core.memory.attachments as attachment_module
import core.memory.confined_filesystem as confined_filesystem_module
from core.memory.attachments import (
    AttachmentBundleInvalidError,
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


def _xlsx_bytes() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("[Content_Types].xml", b"content types")
        archive.writestr("xl/workbook.xml", b"workbook")
    return payload.getvalue()


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


def test_pin_store_uses_one_physical_home_through_a_symlinked_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_home = tmp_path / "volume" / "user" / ".avibe"
    source_root = physical_home / "attachments" / "avibe"
    source_root.mkdir(parents=True, mode=0o700)
    source = _source_file(source_root, "notes.txt", b"physical source")
    logical_parent = tmp_path / "home"
    logical_parent.symlink_to(tmp_path / "volume", target_is_directory=True)
    logical_home = logical_parent / "user" / ".avibe"
    monkeypatch.setattr(
        attachment_module.paths,
        "get_vibe_remote_dir",
        lambda: logical_home,
    )

    store = AttachmentPinStore(
        effective_home=logical_home,
        source_root=logical_home / "attachments" / "avibe",
    )
    converted = workbench_capture_attachments(
        [
            SimpleNamespace(
                name=source.name,
                mimetype="text/plain",
                local_path=str(source),
            )
        ]
    )
    bundle = store.pin(converted)

    assert converted == (_attachment(source),)
    assert store._effective_home == physical_home
    assert store._root == physical_home / "memory" / "attachments"
    assert store.provider_attachments(bundle)[0].uri.startswith(
        (physical_home / "memory" / "attachments").as_uri()
    )
    store.release(bundle.bundle_id)


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


@pytest.mark.parametrize("replacement", ["regular", "symlink"])
def test_pin_revalidates_workbench_office_copy_without_losing_siblings(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    home, source_root = attachment_roots
    monkeypatch.setattr(
        "core.memory.modality.office_conversion_available",
        lambda: True,
    )
    office = _source_file(source_root, "report.xlsx", _xlsx_bytes())
    notes = _source_file(source_root, "notes.txt", b"keep this sibling")
    converted = workbench_capture_attachments(
        [
            SimpleNamespace(
                name="report.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                local_path=str(office),
            ),
            SimpleNamespace(
                name="notes.txt",
                mimetype="text/plain",
                local_path=str(notes),
            ),
        ]
    )
    assert [attachment.name for attachment in converted] == [
        "report.xlsx",
        "notes.txt",
    ]

    if replacement == "regular":
        office.write_bytes(b"not an Office container")
    else:
        office.unlink()
        office.symlink_to(notes)

    store = AttachmentPinStore()
    bundle = store.pin(converted)

    assert [attachment.name for attachment in bundle.attachments] == ["notes.txt"]
    assert bundle.attachments[0].storage_key.endswith("/00.txt")
    assert store.provider_attachments(bundle)[0].name == "notes.txt"
    staged = home / "memory" / "attachments" / "staging"
    assert list(staged.iterdir()) == []
    store.release(bundle.bundle_id)


def test_workbench_long_office_display_name_keeps_suffix_through_pin(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    monkeypatch.setattr(
        "core.memory.modality.office_conversion_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "core.memory.modality.office_document_conversion_succeeds",
        lambda _path, **_kwargs: True,
    )
    office = _source_file(source_root, "upload.xlsx", _xlsx_bytes())
    long_name = f"{'report' * 100}.xlsx"

    converted = workbench_capture_attachments(
        [
            SimpleNamespace(
                name=long_name,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                local_path=str(office),
            )
        ]
    )
    bundle = AttachmentPinStore().pin(converted)

    assert len(converted[0].name.encode("utf-8")) <= 512
    assert converted[0].name.endswith(".xlsx")
    assert bundle.attachments[0].name == converted[0].name


def test_pin_requires_office_conversion_proof_without_losing_siblings(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    monkeypatch.setattr(
        "core.memory.modality.office_conversion_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "core.memory.modality.office_document_conversion_succeeds",
        lambda _path, **_kwargs: False,
    )
    office = _source_file(source_root, "report.xlsx", _xlsx_bytes())
    notes = _source_file(source_root, "notes.txt", b"keep this sibling")
    converted = workbench_capture_attachments(
        [
            SimpleNamespace(
                name="report.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                local_path=str(office),
            ),
            SimpleNamespace(
                name="notes.txt",
                mimetype="text/plain",
                local_path=str(notes),
            ),
        ]
    )

    bundle = AttachmentPinStore().pin(converted)

    assert [attachment.name for attachment in bundle.attachments] == ["notes.txt"]
    assert bundle.attachments[0].storage_key.endswith("/00.txt")


def test_office_conversion_uses_one_bounded_budget_per_pin(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    monkeypatch.setattr(
        "core.memory.modality.office_conversion_available",
        lambda: True,
    )
    observed_timeouts: list[float] = []

    def conversion_succeeds(_path: Path, *, timeout_seconds: float) -> bool:
        observed_timeouts.append(timeout_seconds)
        return True

    monkeypatch.setattr(
        "core.memory.modality.office_document_conversion_succeeds",
        conversion_succeeds,
    )
    clock = iter([100.0, 105.0, 131.0])
    monkeypatch.setattr(attachment_module.time, "monotonic", lambda: next(clock))
    first = _source_file(source_root, "first.xlsx", _xlsx_bytes())
    second = _source_file(source_root, "second.xlsx", _xlsx_bytes())
    converted = workbench_capture_attachments(
        [
            SimpleNamespace(
                name=path.name,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                local_path=str(path),
            )
            for path in (first, second)
        ]
    )

    bundle = AttachmentPinStore().pin(converted)

    assert [attachment.name for attachment in bundle.attachments] == ["first.xlsx"]
    assert observed_timeouts == [25.0]


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
    source_info = source.stat()
    store = AttachmentPinStore()
    real_fstat = os.fstat

    def unowned_source_fstat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) != (source_info.st_dev, source_info.st_ino):
            return info
        values = list(info)
        values[4] = os.getuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(attachment_module.os, "fstat", unowned_source_fstat)
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
    assert isinstance(error.value, AttachmentBundleInvalidError)


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
    assert isinstance(error.value, AttachmentBundleInvalidError)


def test_attachment_reads_translate_temporary_ordering_database_failure(
    attachment_roots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home, source_root = attachment_roots
    store = AttachmentPinStore()
    bundle = store.pin((_attachment(_source_file(source_root, "referenced.txt")),))
    failure = sqlite3.OperationalError("temporary ordering unavailable")

    def fail_connect(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(
        confined_filesystem_module.sqlite3,
        "connect",
        fail_connect,
    )

    with pytest.raises(AttachmentPinError) as raised:
        store.provider_attachments(bundle)

    _assert_pin_error(raised, "memory_store_unavailable")
    assert not isinstance(raised.value, AttachmentBundleInvalidError)
    assert isinstance(
        raised.value.__cause__,
        confined_filesystem_module.ConfinedFilesystemError,
    )
    assert raised.value.__cause__.__cause__ is failure
def test_clear_all_removes_safe_entries_without_trusting_their_names(
    attachment_roots,
) -> None:
    home, source_root = attachment_roots
    store = AttachmentPinStore()
    store.pin((_attachment(_source_file(source_root, "valid.txt")),))
    root = home / "memory" / "attachments"

    malformed_bundle = root / "bundles" / "NOT-A-BUNDLE"
    malformed_bundle.mkdir(mode=0o700)
    malformed_file = malformed_bundle / "not-a-pinned-filename.bin"
    malformed_file.write_bytes(b"malformed but confined")
    malformed_file.chmod(0o600)
    loose_bundle_file = root / "bundles" / "loose bytes"
    loose_bundle_file.write_bytes(b"loose but confined")
    loose_bundle_file.chmod(0o600)
    loose_staging_file = root / "staging" / "not-a-stage-name"
    loose_staging_file.write_bytes(b"partial")
    loose_staging_file.chmod(0o600)

    store.clear_all()
    store.clear_all()

    assert list((root / "bundles").iterdir()) == []
    assert list((root / "staging").iterdir()) == []


@pytest.mark.parametrize("unsafe_entry", ["bundle_symlink", "file_symlink", "special"])
def test_clear_all_rejects_unsafe_entries_without_touching_outside(
    attachment_roots,
    unsafe_entry: str,
) -> None:
    home, _source_root = attachment_roots
    store = AttachmentPinStore()
    root = home / "memory" / "attachments"
    outside = home / "outside-attachments"
    outside.mkdir(mode=0o700)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside must remain")

    if unsafe_entry == "bundle_symlink":
        unsafe = root / "bundles" / "malformed-bundle"
        unsafe.symlink_to(outside, target_is_directory=True)
    else:
        malformed_bundle = root / "bundles" / "malformed-bundle"
        malformed_bundle.mkdir(mode=0o700)
        unsafe = malformed_bundle / "malformed-entry"
        if unsafe_entry == "file_symlink":
            unsafe.symlink_to(sentinel)
        else:
            if not hasattr(os, "mkfifo"):
                pytest.skip("FIFO creation is unavailable")
            os.mkfifo(unsafe, mode=0o600)

    with pytest.raises(AttachmentPinError) as error:
        store.clear_all()

    _assert_pin_error(error, "memory_store_unavailable")
    assert unsafe.exists() or unsafe.is_symlink()
    assert sentinel.read_bytes() == b"outside must remain"


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
