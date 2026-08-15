"""Private, durable pinning for Workbench Memory attachments."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes, urlsplit

from config import paths
from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    SpilledDirectoryOrder,
    required_no_follow_flag,
)
from core.memory.modality import SUPPORTED_ATTACHMENT_EXTENSIONS
from core.memory.types import (
    CaptureAttachment,
    MemoryContentKind,
    MemoryErrorCode,
)

if TYPE_CHECKING:
    from core.handlers.inbound_attachments import (
        InboundAttachmentLease,
        _LeasedAttachmentRecord,
    )


MAX_PINNED_ATTACHMENTS = 8
MAX_PINNED_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PINNED_BUNDLE_BYTES = 100 * 1024 * 1024
# Delivery gate: Slack is the reference platform in PR4. Remaining adapters
# acquire their native classification in PR5 before admission expands to them.
IM_ATTACHMENT_CAPTURE_AVAILABLE = True

_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ATTACHMENT_NAME_BYTES = 512
_MAX_FILE_URI_BYTES = 8 * 1024
_BUNDLE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EXTENSION_PATTERN = re.compile(r"[a-z0-9]{1,8}")
_BUNDLE_FILENAME_PATTERN = re.compile(r"(0[0-7])\.([a-z0-9]{1,8})")
_STAGING_NAME_PATTERN = re.compile(r"([0-9a-f]{32})\.tmp")
_VALID_KINDS = frozenset({"image", "audio", "doc", "pdf", "html", "email"})
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def attachment_pin_root(effective_home: Path | str | None = None) -> Path:
    """Return the sole provider-visible root for pinned Memory attachments."""

    home = paths.get_vibe_remote_dir() if effective_home is None else effective_home
    return _absolute_lexical(home) / "memory" / "attachments"


class AttachmentPinError(OSError):
    """A closed attachment admission or durable-storage failure."""

    def __init__(self, error: MemoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True, slots=True)
class PinnedAttachment:
    """Persistable metadata for one file in an Avibe-owned bundle."""

    kind: MemoryContentKind
    name: str
    ext: str
    storage_key: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not _valid_kind_name_extension(self.kind, self.name, self.ext):
            raise ValueError("invalid pinned attachment metadata")
        if not _valid_storage_key_shape(self.storage_key, self.ext):
            raise ValueError("invalid attachment storage key")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= MAX_PINNED_ATTACHMENT_BYTES
            or not isinstance(self.sha256, str)
            or _SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            raise ValueError("invalid pinned attachment metadata")


@dataclass(frozen=True, slots=True)
class PinnedBundle:
    """One atomically published attachment bundle with no absolute paths."""

    bundle_id: str
    attachments: tuple[PinnedAttachment, ...]
    total_bytes: int

    def __post_init__(self) -> None:
        if not _valid_bundle_id(self.bundle_id):
            raise ValueError("invalid attachment bundle id")
        if (
            not isinstance(self.attachments, tuple)
            or not 1 <= len(self.attachments) <= MAX_PINNED_ATTACHMENTS
            or any(not isinstance(item, PinnedAttachment) for item in self.attachments)
            or isinstance(self.total_bytes, bool)
            or not isinstance(self.total_bytes, int)
            or self.total_bytes != sum(item.size_bytes for item in self.attachments)
            or self.total_bytes > MAX_PINNED_BUNDLE_BYTES
        ):
            raise ValueError("invalid attachment bundle metadata")
        for index, attachment in enumerate(self.attachments):
            if attachment.storage_key != _storage_key(self.bundle_id, index, attachment.ext):
                raise ValueError("invalid attachment storage key")

    @property
    def relative_path(self) -> str:
        return PurePosixPath("bundles", self.bundle_id).as_posix()


def encode_pinned_bundle(bundle: PinnedBundle) -> str:
    """Encode only the portable manifest metadata stored in the outbox row."""

    return json.dumps(
        {
            "version": 1,
            "total_bytes": bundle.total_bytes,
            "attachments": [
                {
                    "kind": item.kind,
                    "name": item.name,
                    "ext": item.ext,
                    "storage_key": item.storage_key,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in bundle.attachments
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_pinned_bundle(bundle_id: str, payload: str) -> PinnedBundle:
    """Decode the exact closed manifest persisted beside a bundle reference."""

    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise AttachmentPinError(
            "memory_store_unavailable",
            "pinned attachment manifest is invalid",
        ) from error
    if not isinstance(value, dict) or set(value) != {"version", "total_bytes", "attachments"}:
        raise AttachmentPinError(
            "memory_store_unavailable",
            "pinned attachment manifest is invalid",
        )
    items = value.get("attachments")
    if value.get("version") != 1 or not isinstance(items, list):
        raise AttachmentPinError(
            "memory_store_unavailable",
            "pinned attachment manifest is invalid",
        )
    try:
        attachments = tuple(
            PinnedAttachment(
                kind=item["kind"],
                name=item["name"],
                ext=item["ext"],
                storage_key=item["storage_key"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            for item in items
            if isinstance(item, dict)
            and set(item) == {
                "kind",
                "name",
                "ext",
                "storage_key",
                "size_bytes",
                "sha256",
            }
        )
        if len(attachments) != len(items):
            raise ValueError("manifest item shape mismatch")
        return PinnedBundle(
            bundle_id=bundle_id,
            attachments=attachments,
            total_bytes=value["total_bytes"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AttachmentPinError(
            "memory_store_unavailable",
            "pinned attachment manifest is invalid",
        ) from error


class AttachmentPinStore:
    """Own private bundle creation, projection, and no-follow reclamation."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        effective_home: Path | str | None = None,
        source_root: Path | None = None,
    ) -> None:
        self._effective_home = _absolute_lexical(
            paths.get_vibe_remote_dir()
            if effective_home is None
            else effective_home
        )
        self._root = (
            _absolute_lexical(root)
            if root is not None
            else attachment_pin_root(self._effective_home)
        )
        self._source_root = _absolute_lexical(
            source_root
            if source_root is not None
            else self._effective_home / "attachments" / "avibe"
        )
        self._staging = self._root / "staging"
        self._bundles = self._root / "bundles"
        self._lock = threading.RLock()
        _require_path_below(self._root, self._effective_home, "attachment storage root")
        _require_path_below(self._source_root, self._effective_home, "attachment source root")
        if _paths_overlap(self._root, self._source_root):
            raise AttachmentPinError(
                "memory_store_unavailable",
                "attachment source and storage roots must be disjoint",
            )
        _required_no_follow_flag()
        with self._lock:
            self._prepare_private_layout()

    def pin(
        self,
        sources: Sequence[CaptureAttachment],
        *,
        source_lease: InboundAttachmentLease | None = None,
    ) -> PinnedBundle:
        """Copy one bounded source set and publish it only after durable rename."""

        try:
            source_items = tuple(sources)
        except TypeError as error:
            raise AttachmentPinError("memory_invalid_input", "attachments are invalid") from error
        self._validate_sources(source_items)
        source_root, allowed_records = self._pin_source(source_lease)
        with self._lock:
            self._verify_private_layout()
            staging_fd = _open_private_directory(self._staging, "attachment staging root")
            bundles_fd = _open_private_directory(self._bundles, "attachment bundles root")
            bundle_id: str | None = None
            stage_name: str | None = None
            renamed = False
            try:
                bundle_id, stage_name = self._create_staging_directory(
                    staging_fd=staging_fd,
                    bundles_fd=bundles_fd,
                )
                stage_fd = _open_private_directory_at(
                    staging_fd,
                    stage_name,
                    "attachment staging bundle",
                )
                pinned: list[PinnedAttachment] = []
                total_bytes = 0
                try:
                    for index, source in enumerate(source_items):
                        source_fd, source_info, source_sha256 = self._open_source(
                            source,
                            source_root=source_root,
                            allowed_records=allowed_records,
                            source_lease=source_lease,
                        )
                        try:
                            filename = _bundle_filename(index, source.ext)
                            size_bytes, digest = _copy_source_file(
                                source_fd,
                                source_info,
                                stage_fd,
                                filename,
                                total_before=total_bytes,
                                expected_sha256=source_sha256,
                            )
                        finally:
                            os.close(source_fd)
                        total_bytes += size_bytes
                        pinned.append(
                            PinnedAttachment(
                                kind=source.kind,
                                name=source.name,
                                ext=source.ext,
                                storage_key=_storage_key(bundle_id, index, source.ext),
                                size_bytes=size_bytes,
                                sha256=digest,
                            )
                        )
                    _fsync_fd(stage_fd, "attachment staging bundle")
                finally:
                    os.close(stage_fd)

                try:
                    os.rename(
                        stage_name,
                        bundle_id,
                        src_dir_fd=staging_fd,
                        dst_dir_fd=bundles_fd,
                    )
                    renamed = True
                    _fsync_fd(staging_fd, "attachment staging root")
                    _fsync_fd(bundles_fd, "attachment bundles root")
                except OSError as error:
                    raise _storage_failure(error, "attachment bundle could not be published") from error
                return PinnedBundle(
                    bundle_id=bundle_id,
                    attachments=tuple(pinned),
                    total_bytes=total_bytes,
                )
            except AttachmentPinError:
                if stage_name is not None:
                    cleanup_parent_fd = bundles_fd if renamed else staging_fd
                    cleanup_name = bundle_id if renamed else stage_name
                    _remove_private_bundle_quietly(
                        cleanup_parent_fd,
                        cleanup_name,
                        strict_files=renamed,
                    )
                raise
            except OSError as error:
                if stage_name is not None:
                    cleanup_parent_fd = bundles_fd if renamed else staging_fd
                    cleanup_name = bundle_id if renamed else stage_name
                    _remove_private_bundle_quietly(
                        cleanup_parent_fd,
                        cleanup_name,
                        strict_files=renamed,
                    )
                raise _storage_failure(error, "attachment bundle could not be pinned") from error
            finally:
                os.close(bundles_fd)
                os.close(staging_fd)

    def provider_attachments(self, bundle: PinnedBundle) -> tuple[CaptureAttachment, ...]:
        """Verify one persisted bundle and project it to provider-only file URIs."""

        try:
            checked = PinnedBundle(
                bundle_id=bundle.bundle_id,
                attachments=bundle.attachments,
                total_bytes=bundle.total_bytes,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise AttachmentPinError(
                "memory_store_unavailable",
                "pinned attachment metadata is invalid",
            ) from error

        with self._lock:
            self._verify_private_layout()
            bundles_fd = _open_private_directory(self._bundles, "attachment bundles root")
            try:
                observed_files = _validate_private_bundle(bundles_fd, checked.bundle_id)
                expected_files = tuple(
                    _bundle_filename(index, pinned.ext)
                    for index, pinned in enumerate(checked.attachments)
                )
                if observed_files != expected_files:
                    raise AttachmentPinError(
                        "memory_store_unavailable",
                        "attachment bundle does not match its manifest",
                    )
                bundle_fd = _open_private_directory_at(
                    bundles_fd,
                    checked.bundle_id,
                    "attachment bundle",
                )
                try:
                    projected: list[CaptureAttachment] = []
                    for index, pinned in enumerate(checked.attachments):
                        filename = _bundle_filename(index, pinned.ext)
                        _verify_pinned_file(bundle_fd, filename, pinned)
                        projected.append(
                            CaptureAttachment(
                                kind=pinned.kind,
                                name=pinned.name,
                                uri=(self._root / Path(pinned.storage_key)).as_uri(),
                                ext=pinned.ext,
                            )
                        )
                    return tuple(projected)
                finally:
                    os.close(bundle_fd)
            finally:
                os.close(bundles_fd)

    def release(self, bundle_id: str) -> None:
        """Idempotently remove one valid bundle without following any entry."""

        if not _valid_bundle_id(bundle_id):
            raise AttachmentPinError("memory_invalid_input", "invalid attachment bundle id")
        with self._lock:
            self._verify_private_layout()
            bundles_fd = _open_private_directory(self._bundles, "attachment bundles root")
            try:
                _remove_private_bundle(bundles_fd, bundle_id, strict_files=True)
            finally:
                os.close(bundles_fd)

    def reconcile(
        self,
        referenced_bundle_ids: Collection[str],
        releasing_bundle_ids: Collection[str],
    ) -> tuple[str, ...]:
        """Remove staging/releasing/orphan bundles while preserving every reference."""

        referenced = _validated_bundle_ids(referenced_bundle_ids)
        releasing = _validated_bundle_ids(releasing_bundle_ids)
        if referenced & releasing:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment bundle cannot be referenced and releasing",
            )

        with self._lock:
            self._verify_private_layout()
            staging_fd = _open_private_directory(self._staging, "attachment staging root")
            bundles_fd = _open_private_directory(self._bundles, "attachment bundles root")
            removed: list[str] = []
            try:
                for name in _directory_entry_names(staging_fd):
                    if _STAGING_NAME_PATTERN.fullmatch(name) is not None:
                        _remove_private_bundle(staging_fd, name, strict_files=False)

                existing: set[str] = set()
                for name in _directory_entry_names(bundles_fd):
                    if not _valid_bundle_id(name):
                        continue
                    existing.add(name)
                    if name in referenced:
                        _validate_private_bundle(bundles_fd, name)
                        continue
                    _remove_private_bundle(bundles_fd, name, strict_files=True)
                    removed.append(name)

                missing = referenced - existing
                if missing:
                    raise AttachmentPinError(
                        "memory_store_unavailable",
                        "a referenced attachment bundle is missing",
                    )
                # A missing releasing bundle is already fully released.
                return tuple(sorted(set(removed)))
            finally:
                os.close(bundles_fd)
                os.close(staging_fd)

    def clear_all(self) -> None:
        """Remove every safely confined entry, regardless of bundle naming."""

        with self._lock:
            self._verify_private_layout()
            staging_fd = _open_private_directory(self._staging, "attachment staging root")
            bundles_fd = _open_private_directory(self._bundles, "attachment bundles root")
            try:
                for directory_fd in (staging_fd, bundles_fd):
                    for name in _directory_entry_names(directory_fd):
                        _remove_private_entry(directory_fd, name)
                    if _directory_entry_names(directory_fd):
                        raise AttachmentPinError(
                            "memory_store_unavailable",
                            "attachment storage could not be fully cleared",
                        )
                    _fsync_fd(directory_fd, "attachment storage root")
            finally:
                os.close(bundles_fd)
                os.close(staging_fd)

    def _prepare_private_layout(self) -> None:
        for directory in (self._root, self._staging, self._bundles):
            _ensure_private_directory(directory)

    def _verify_private_layout(self) -> None:
        for directory, label in (
            (self._root, "attachment storage root"),
            (self._staging, "attachment staging root"),
            (self._bundles, "attachment bundles root"),
        ):
            descriptor = _open_private_directory(directory, label)
            os.close(descriptor)

    def _validate_sources(self, sources: tuple[CaptureAttachment, ...]) -> None:
        if not 1 <= len(sources) <= MAX_PINNED_ATTACHMENTS:
            raise AttachmentPinError(
                "memory_input_too_large",
                "attachment count exceeds the capture limit",
            )
        for source in sources:
            if (
                not isinstance(source, CaptureAttachment)
                or not _valid_kind_name_extension(source.kind, source.name, source.ext)
                or not isinstance(source.uri, str)
                or not source.uri
            ):
                raise AttachmentPinError("memory_invalid_input", "attachment metadata is invalid")

    def _create_staging_directory(self, *, staging_fd: int, bundles_fd: int) -> tuple[str, str]:
        for _ in range(100):
            bundle_id = secrets.token_hex(16)
            stage_name = f"{bundle_id}.tmp"
            if _entry_exists(bundles_fd, bundle_id):
                continue
            try:
                os.mkdir(stage_name, mode=0o700, dir_fd=staging_fd)
            except FileExistsError:
                continue
            except OSError as error:
                raise _storage_failure(error, "attachment staging bundle could not be created") from error
            try:
                stage_fd = _open_directory_at(
                    staging_fd,
                    stage_name,
                    "attachment staging bundle",
                )
                try:
                    os.fchmod(stage_fd, 0o700)
                    _require_private_directory(
                        os.fstat(stage_fd),
                        "attachment staging bundle",
                    )
                    _fsync_fd(stage_fd, "attachment staging bundle")
                finally:
                    os.close(stage_fd)
                _fsync_fd(staging_fd, "attachment staging root")
            except AttachmentPinError:
                _remove_private_bundle_quietly(staging_fd, stage_name, strict_files=False)
                raise
            return bundle_id, stage_name
        raise AttachmentPinError(
            "memory_store_unavailable",
            "attachment staging bundle could not be reserved",
        )

    def _pin_source(
        self,
        source_lease: InboundAttachmentLease | None,
    ) -> tuple[Path, dict[Path, _LeasedAttachmentRecord] | None]:
        if source_lease is None:
            return self._source_root, None
        from core.handlers.inbound_attachments import (
            InboundAttachmentLease,
            leased_attachment_records,
        )

        if type(source_lease) is not InboundAttachmentLease:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment source lease is invalid",
            )
        directory_fd: int | None = None
        try:
            root, directory_fd, records = leased_attachment_records(source_lease)
        except (TypeError, ValueError) as error:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment source lease is invalid",
            ) from error
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
        expected_root = self._effective_home / "attachments" / "im"
        if _absolute_lexical(root) != expected_root:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment source lease belongs to another Avibe home",
            )
        _require_path_below(root, self._effective_home, "leased attachment source root")
        if _paths_overlap(self._root, root):
            raise AttachmentPinError(
                "memory_store_unavailable",
                "attachment source and storage roots must be disjoint",
            )
        return root, {record.path: record for record in records}

    def _open_source(
        self,
        source: CaptureAttachment,
        *,
        source_root: Path,
        allowed_records: dict[Path, _LeasedAttachmentRecord] | None,
        source_lease: InboundAttachmentLease | None,
    ) -> tuple[int, os.stat_result, str | None]:
        source_path = _path_from_file_uri(source.uri)
        if allowed_records is not None and source_path not in allowed_records:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment is not part of the source lease",
            )
        try:
            relative = source_path.relative_to(source_root)
        except ValueError as error:
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment is outside the source root",
            ) from error
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise AttachmentPinError("memory_invalid_input", "attachment source path is invalid")
        if source_path.suffix.lstrip(".").lower() != source.ext:
            raise AttachmentPinError("memory_invalid_input", "attachment extension is inconsistent")

        if source_lease is not None:
            from core.handlers.inbound_attachments import (
                leased_attachment_records,
                open_leased_attachment_record,
            )

            directory_fd: int | None = None
            try:
                current_root, directory_fd, current_records = leased_attachment_records(source_lease)
                current = {record.path: record for record in current_records}.get(source_path)
                if current_root != source_root or current != allowed_records[source_path]:
                    raise ValueError("attachment source lease changed")
                descriptor, info = open_leased_attachment_record(directory_fd, current)
                return descriptor, info, current.sha256
            except (TypeError, ValueError) as error:
                raise AttachmentPinError(
                    "memory_invalid_input",
                    "attachment is not part of the source lease",
                ) from error
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)

        current_fd = _open_source_directory_path(source_root)
        try:
            for component in relative.parts[:-1]:
                next_fd = _open_source_directory_at(current_fd, component)
                os.close(current_fd)
                current_fd = next_fd
            file_fd = _open_source_file_at(current_fd, relative.parts[-1])
        finally:
            os.close(current_fd)
        try:
            return file_fd, os.fstat(file_fd), None
        except OSError as error:
            os.close(file_fd)
            raise AttachmentPinError(
                "memory_invalid_input",
                "attachment source is unavailable",
            ) from error


def workbench_capture_attachments(files: object) -> tuple[CaptureAttachment, ...]:
    """Convert supported Workbench uploads without erasing symlink evidence."""

    if not isinstance(files, list):
        return ()
    source_root = _absolute_lexical(paths.get_attachments_dir() / "avibe")
    converted: list[CaptureAttachment] = []
    for file in files:
        local_path = getattr(file, "local_path", None)
        name = getattr(file, "name", None)
        mimetype = getattr(file, "mimetype", None)
        if not all(isinstance(value, str) and value for value in (local_path, name, mimetype)):
            continue
        try:
            path = Path(local_path)
            if not path.is_absolute():
                continue
            relative = path.relative_to(source_root)
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                continue
        except (OSError, ValueError):
            continue
        extension = path.suffix.lstrip(".").lower()
        if _EXTENSION_PATTERN.fullmatch(extension) is None:
            continue
        if extension not in SUPPORTED_ATTACHMENT_EXTENSIONS:
            # The provider answers an unparseable extension with a permanent
            # rejection, so an upload it cannot read never becomes a capture.
            continue
        normalized_mime = mimetype.lower().split(";", 1)[0].strip()
        if normalized_mime.startswith("image/"):
            kind: MemoryContentKind = "image"
        elif normalized_mime.startswith("audio/"):
            kind = "audio"
        elif normalized_mime == "application/pdf" or extension == "pdf":
            kind = "pdf"
        elif normalized_mime == "text/html" or extension in {"html", "htm"}:
            kind = "html"
        elif normalized_mime == "message/rfc822" or extension == "eml":
            kind = "email"
        else:
            kind = "doc"
        display_name = Path(name).name
        try:
            encoded_name = display_name.encode("utf-8")
        except UnicodeError:
            continue
        if len(encoded_name) > _MAX_ATTACHMENT_NAME_BYTES:
            display_name = encoded_name[:_MAX_ATTACHMENT_NAME_BYTES].decode(
                "utf-8",
                errors="ignore",
            )
        if display_name:
            converted.append(
                CaptureAttachment(
                    kind=kind,
                    name=display_name,
                    uri=path.as_uri(),
                    ext=extension,
                )
            )
    return tuple(converted)


def _absolute_lexical(value: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _require_path_below(path: Path, parent: Path, label: str) -> None:
    try:
        relative = path.relative_to(parent)
    except ValueError as error:
        raise AttachmentPinError(
            "memory_store_unavailable",
            f"{label} must stay inside the effective Avibe home",
        ) from error
    if not relative.parts:
        raise AttachmentPinError(
            "memory_store_unavailable",
            f"{label} must be below the effective Avibe home",
        )


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _required_no_follow_flag() -> int:
    try:
        return required_no_follow_flag()
    except ConfinedFilesystemError as error:
        raise AttachmentPinError(
            "memory_store_unavailable",
            "durable attachments require no-follow filesystem support",
        ) from error


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | _required_no_follow_flag()
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _file_read_flags() -> int:
    return (
        os.O_RDONLY
        | _required_no_follow_flag()
        | int(getattr(os, "O_NONBLOCK", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _file_write_flags() -> int:
    return (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _required_no_follow_flag()
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _ensure_private_directory(directory: Path) -> None:
    if not directory.is_absolute():
        raise AttachmentPinError("memory_store_unavailable", "attachment storage path must be absolute")
    try:
        descriptor = os.open(directory.anchor, _directory_open_flags())
    except OSError as error:
        raise _storage_failure(error, "attachment storage anchor is unavailable") from error
    try:
        for component in directory.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    _fsync_fd(descriptor, "attachment directory parent")
                except FileExistsError:
                    pass
                except AttachmentPinError:
                    raise
                except OSError as error:
                    raise _storage_failure(
                        error,
                        "attachment directory could not be created",
                    ) from error
                try:
                    next_descriptor = os.open(
                        component,
                        _directory_open_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise _storage_failure(
                        error,
                        "attachment directory is unavailable",
                    ) from error
            except OSError as error:
                raise _storage_failure(
                    error,
                    "attachment storage path contains an unsafe directory component",
                ) from error
            os.close(descriptor)
            descriptor = next_descriptor

        _require_current_owner(os.fstat(descriptor), "attachment storage directory", storage=True)
        try:
            os.fchmod(descriptor, 0o700)
        except OSError as error:
            raise _storage_failure(error, "attachment directory could not be made private") from error
        _require_private_directory(os.fstat(descriptor), "attachment storage directory")
        _fsync_fd(descriptor, "attachment storage directory")
    finally:
        os.close(descriptor)


def _open_directory_no_follow(path: Path, label: str) -> int:
    if not path.is_absolute():
        raise AttachmentPinError("memory_store_unavailable", f"{label} path must be absolute")
    try:
        descriptor = os.open(path.anchor, _directory_open_flags())
    except OSError as error:
        raise _storage_failure(error, f"{label} is unavailable") from error
    try:
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise _storage_failure(error, f"{label} is unavailable") from error
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise AttachmentPinError("memory_store_unavailable", f"{label} is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_directory(path: Path, label: str) -> int:
    descriptor = _open_directory_no_follow(path, label)
    try:
        _require_private_directory(os.fstat(descriptor), label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_directory_at(parent_fd: int, name: str, label: str) -> int:
    descriptor = _open_directory_at(parent_fd, name, label)
    try:
        _require_private_directory(os.fstat(descriptor), label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_at(parent_fd: int, name: str, label: str) -> int:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise _storage_failure(error, f"{label} is unavailable") from error
    return descriptor


def _open_source_directory_path(path: Path) -> int:
    try:
        descriptor = _open_directory_no_follow(path, "attachment source root")
    except AttachmentPinError as error:
        raise AttachmentPinError("memory_invalid_input", "attachment source root is unavailable") from error
    try:
        _require_safe_source_directory(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_source_directory_at(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise AttachmentPinError("memory_invalid_input", "attachment parent is unsafe") from error
    try:
        _require_safe_source_directory(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_source_file_at(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _file_read_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise AttachmentPinError("memory_invalid_input", "attachment source is unavailable") from error
    try:
        _require_safe_source_file(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_current_owner(info: os.stat_result, label: str, *, storage: bool) -> None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        error: MemoryErrorCode = "memory_store_unavailable" if storage else "memory_invalid_input"
        raise AttachmentPinError(error, f"{label} has an unexpected owner")


def _require_private_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise AttachmentPinError("memory_store_unavailable", f"{label} is not private")
    _require_current_owner(info, label, storage=True)


def _require_private_file(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise AttachmentPinError("memory_store_unavailable", f"{label} is not private")
    _require_current_owner(info, label, storage=True)


def _require_safe_source_directory(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
        raise AttachmentPinError("memory_invalid_input", "attachment parent is unsafe")
    _require_current_owner(info, "attachment parent", storage=False)


def _require_safe_source_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
        raise AttachmentPinError("memory_invalid_input", "attachment source is unsafe")
    _require_current_owner(info, "attachment source", storage=False)


def _copy_source_file(
    source_fd: int,
    source_info: os.stat_result,
    destination_dir_fd: int,
    filename: str,
    *,
    total_before: int,
    expected_sha256: str | None,
) -> tuple[int, str]:
    if source_info.st_size > MAX_PINNED_ATTACHMENT_BYTES:
        raise AttachmentPinError("memory_input_too_large", "attachment exceeds the file size limit")
    if total_before + source_info.st_size > MAX_PINNED_BUNDLE_BYTES:
        raise AttachmentPinError("memory_input_too_large", "attachments exceed the capture size limit")
    try:
        destination_fd = os.open(
            filename,
            _file_write_flags(),
            0o600,
            dir_fd=destination_dir_fd,
        )
    except OSError as error:
        raise _storage_failure(error, "pinned attachment could not be created") from error

    digest = hashlib.sha256()
    copied = 0
    try:
        try:
            os.fchmod(destination_fd, 0o600)
            _require_private_file(os.fstat(destination_fd), "pinned attachment")
            while True:
                chunk = _read_source_chunk(source_fd)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_PINNED_ATTACHMENT_BYTES:
                    raise AttachmentPinError(
                        "memory_input_too_large",
                        "attachment exceeds the file size limit",
                    )
                if total_before + copied > MAX_PINNED_BUNDLE_BYTES:
                    raise AttachmentPinError(
                        "memory_input_too_large",
                        "attachments exceed the capture size limit",
                    )
                digest.update(chunk)
                _write_all(destination_fd, chunk)
            try:
                after = os.fstat(source_fd)
            except OSError as error:
                raise AttachmentPinError(
                    "memory_invalid_input",
                    "attachment source is unavailable",
                ) from error
            if copied != source_info.st_size or not _same_source_file(source_info, after):
                raise AttachmentPinError(
                    "memory_invalid_input",
                    "attachment source changed while it was pinned",
                )
            observed_sha256 = digest.hexdigest()
            if expected_sha256 is not None and observed_sha256 != expected_sha256:
                raise AttachmentPinError(
                    "memory_invalid_input",
                    "attachment source content changed after materialization",
                )
            destination_info = os.fstat(destination_fd)
            _require_private_file(destination_info, "pinned attachment")
            if destination_info.st_size != copied:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "pinned attachment has an unexpected size",
                )
            _fsync_fd(destination_fd, "pinned attachment")
        except AttachmentPinError:
            raise
        except OSError as error:
            raise _storage_failure(error, "pinned attachment could not be written") from error
    finally:
        os.close(destination_fd)
    return copied, observed_sha256


def _read_source_chunk(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, _COPY_CHUNK_BYTES)
    except OSError as error:
        raise AttachmentPinError(
            "memory_invalid_input",
            "attachment source could not be read",
        ) from error


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "attachment write made no progress")
        view = view[written:]


def _same_source_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
        stat.S_IMODE(before.st_mode),
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        stat.S_IFMT(after.st_mode),
        stat.S_IMODE(after.st_mode),
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _verify_pinned_file(bundle_fd: int, filename: str, pinned: PinnedAttachment) -> None:
    try:
        descriptor = os.open(filename, _file_read_flags(), dir_fd=bundle_fd)
    except OSError as error:
        raise _storage_failure(error, "pinned attachment is unavailable") from error
    try:
        before = os.fstat(descriptor)
        _require_private_file(before, "pinned attachment")
        if before.st_size != pinned.size_bytes:
            raise AttachmentPinError(
                "memory_store_unavailable",
                "pinned attachment has an unexpected size",
            )
        digest = hashlib.sha256()
        read_bytes = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            read_bytes += len(chunk)
            if read_bytes > pinned.size_bytes:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "pinned attachment has an unexpected size",
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            read_bytes != pinned.size_bytes
            or digest.hexdigest() != pinned.sha256
            or not _same_source_file(before, after)
        ):
            raise AttachmentPinError(
                "memory_store_unavailable",
                "pinned attachment integrity check failed",
            )
    except AttachmentPinError:
        raise
    except OSError as error:
        raise _storage_failure(error, "pinned attachment could not be verified") from error
    finally:
        os.close(descriptor)


def _path_from_file_uri(uri: str) -> Path:
    try:
        if len(uri.encode("utf-8")) > _MAX_FILE_URI_BYTES:
            raise ValueError
    except UnicodeError as error:
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid") from error
    try:
        parsed = urlsplit(uri)
    except ValueError as error:
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid") from error
    if (
        parsed.scheme.lower() != "file"
        or parsed.netloc.lower() not in {"", "localhost"}
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or _INVALID_PERCENT_ESCAPE.search(parsed.path) is not None
    ):
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid")
    try:
        decoded = os.fsdecode(unquote_to_bytes(parsed.path))
    except (UnicodeError, ValueError) as error:
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid") from error
    if "\x00" in decoded:
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid")
    normalized_for_segments = decoded
    if os.altsep is not None:
        normalized_for_segments = normalized_for_segments.replace(os.altsep, os.sep)
    if any(segment in {".", ".."} for segment in normalized_for_segments.split(os.sep)):
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid")
    source_path = Path(decoded)
    if not source_path.is_absolute() or any(part == ".." for part in source_path.parts):
        raise AttachmentPinError("memory_invalid_input", "attachment URI is invalid")
    return source_path


def _storage_key(bundle_id: str, index: int, extension: str) -> str:
    return PurePosixPath("bundles", bundle_id, _bundle_filename(index, extension)).as_posix()


def _bundle_filename(index: int, extension: str) -> str:
    return f"{index:02d}.{extension}"


def _valid_kind_name_extension(kind: object, name: object, extension: object) -> bool:
    if (
        not isinstance(kind, str)
        or kind not in _VALID_KINDS
        or not isinstance(name, str)
        or not name
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or not isinstance(extension, str)
        or _EXTENSION_PATTERN.fullmatch(extension) is None
        or extension not in SUPPORTED_ATTACHMENT_EXTENSIONS
    ):
        return False
    try:
        return len(name.encode("utf-8")) <= _MAX_ATTACHMENT_NAME_BYTES
    except UnicodeError:
        return False


def _valid_storage_key_shape(storage_key: object, extension: str) -> bool:
    if not isinstance(storage_key, str) or not storage_key:
        return False
    key = PurePosixPath(storage_key)
    if key.is_absolute() or key.as_posix() != storage_key or len(key.parts) != 3:
        return False
    prefix, bundle_id, filename = key.parts
    return (
        prefix == "bundles"
        and _valid_bundle_id(bundle_id)
        and filename in {_bundle_filename(index, extension) for index in range(MAX_PINNED_ATTACHMENTS)}
    )


def _valid_bundle_id(value: object) -> bool:
    return isinstance(value, str) and _BUNDLE_ID_PATTERN.fullmatch(value) is not None


def _validated_bundle_ids(values: Collection[str]) -> set[str]:
    try:
        checked = set(values)
    except TypeError as error:
        raise AttachmentPinError("memory_invalid_input", "invalid attachment bundle ids") from error
    if any(not _valid_bundle_id(value) for value in checked):
        raise AttachmentPinError("memory_invalid_input", "invalid attachment bundle id")
    return checked


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _storage_failure(error, "attachment directory entry could not be inspected") from error
    return True


def _directory_entry_names(directory_fd: int) -> tuple[str, ...]:
    try:
        with os.scandir(directory_fd) as entries:
            return tuple(entry.name for entry in entries)
    except OSError as error:
        raise _storage_failure(error, "attachment directory could not be scanned") from error


def _validate_private_bundle(parent_fd: int, name: str) -> tuple[str, ...]:
    bundle_fd = _open_private_directory_at(parent_fd, name, "attachment bundle")
    try:
        filenames = _bounded_ordered_directory_entry_names(
            bundle_fd,
            maximum=MAX_PINNED_ATTACHMENTS,
        )
        if not 1 <= len(filenames) <= MAX_PINNED_ATTACHMENTS:
            raise AttachmentPinError(
                "memory_store_unavailable",
                "attachment bundle has an invalid file count",
            )
        total_bytes = 0
        for index, filename in enumerate(filenames):
            match = _BUNDLE_FILENAME_PATTERN.fullmatch(filename)
            if (
                match is None
                or match.group(1) != f"{index:02d}"
                or match.group(2) not in SUPPORTED_ATTACHMENT_EXTENSIONS
            ):
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "attachment bundle contains an unexpected file",
                )
            try:
                info = os.stat(filename, dir_fd=bundle_fd, follow_symlinks=False)
            except OSError as error:
                raise _storage_failure(error, "attachment bundle entry could not be inspected") from error
            _require_private_file(info, "pinned attachment")
            if info.st_size > MAX_PINNED_ATTACHMENT_BYTES:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "attachment bundle contains an oversized file",
                )
            total_bytes += info.st_size
            if total_bytes > MAX_PINNED_BUNDLE_BYTES:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "attachment bundle exceeds its size limit",
                )
        return tuple(filenames)
    finally:
        os.close(bundle_fd)


def _bounded_ordered_directory_entry_names(
    directory_fd: int,
    *,
    maximum: int,
) -> tuple[str, ...]:
    try:
        with SpilledDirectoryOrder() as orders:
            cursor = orders.scan(directory_fd)
            names: list[str] = []
            while len(names) <= maximum:
                name = orders.next_name(cursor)
                if name is None:
                    break
                names.append(name)
            return tuple(names)
    except ConfinedFilesystemError as error:
        raise AttachmentPinError(
            "memory_store_unavailable",
            "attachment directory could not be ordered safely",
        ) from error


def _remove_private_bundle(parent_fd: int, name: str, *, strict_files: bool) -> None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _storage_failure(error, "attachment bundle could not be inspected") from error
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AttachmentPinError(
            "memory_store_unavailable",
            "attachment bundle path is not a private directory",
        )
    _require_current_owner(info, "attachment bundle", storage=True)
    if stat.S_IMODE(info.st_mode) != 0o700:
        if strict_files:
            raise AttachmentPinError(
                "memory_store_unavailable",
                "attachment bundle path is not private",
            )
        try:
            os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise _storage_failure(error, "attachment staging bundle could not be made private") from error
        _require_private_directory(info, "attachment staging bundle")

    bundle_fd = _open_private_directory_at(parent_fd, name, "attachment bundle")
    try:
        filenames = _directory_entry_names(bundle_fd)
        for filename in filenames:
            try:
                child = os.stat(filename, dir_fd=bundle_fd, follow_symlinks=False)
            except OSError as error:
                raise _storage_failure(error, "attachment bundle entry could not be inspected") from error
            if not stat.S_ISREG(child.st_mode) or stat.S_ISLNK(child.st_mode):
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "attachment bundle contains an unsafe entry",
                )
            _require_current_owner(child, "attachment bundle entry", storage=True)
            if strict_files and stat.S_IMODE(child.st_mode) != 0o600:
                raise AttachmentPinError(
                    "memory_store_unavailable",
                    "attachment bundle contains a non-private file",
                )
        for filename in filenames:
            try:
                os.unlink(filename, dir_fd=bundle_fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _storage_failure(error, "attachment bundle entry could not be removed") from error
        _fsync_fd(bundle_fd, "attachment bundle")
    finally:
        os.close(bundle_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
        _fsync_fd(parent_fd, "attachment bundle parent")
    except FileNotFoundError:
        return
    except OSError as error:
        raise _storage_failure(error, "attachment bundle could not be removed") from error


def _remove_private_entry(parent_fd: int, name: str) -> None:
    """Remove one anchored regular file or flat private directory without following links."""

    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _storage_failure(error, "attachment entry could not be inspected") from error
    _require_current_owner(info, "attachment entry", storage=True)
    if stat.S_ISREG(info.st_mode):
        try:
            os.unlink(name, dir_fd=parent_fd)
            _fsync_fd(parent_fd, "attachment entry parent")
            return
        except FileNotFoundError:
            return
        except OSError as error:
            raise _storage_failure(error, "attachment entry could not be removed") from error
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
        _remove_private_bundle(parent_fd, name, strict_files=False)
        return
    raise AttachmentPinError(
        "memory_store_unavailable",
        "attachment storage contains an unsafe entry",
    )


def _remove_private_bundle_quietly(parent_fd: int, name: str, *, strict_files: bool) -> None:
    try:
        _remove_private_bundle(parent_fd, name, strict_files=strict_files)
    except (AttachmentPinError, OSError):
        return


def _fsync_fd(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise _storage_failure(error, f"{label} could not be synchronized") from error


def _storage_failure(error: OSError, message: str) -> AttachmentPinError:
    code: MemoryErrorCode = (
        "memory_low_disk_space"
        if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
        else "memory_store_unavailable"
    )
    return AttachmentPinError(code, message)
