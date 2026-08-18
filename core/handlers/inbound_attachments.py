"""Shared, leased materialization for native inbound attachments."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import paths
from core.audio_asr import AUDIO_SIGNATURE_SAMPLE_BYTES, detect_audio_mime_from_sample
from modules.im.base import FileAttachment, FileDownloadResult, MessageContext
from vibe.i18n import t as i18n_t


_MAX_FILENAME_BYTES = 200
_SAFE_FILENAME = re.compile(r"[^\w.\-]", re.UNICODE)
_LEASE_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class _LeasedAttachmentRecord:
    name: str
    mimetype: str
    declared_size: int | None
    size: int
    path: Path
    device: int
    inode: int
    sha256: str


class _LeaseState:
    def __init__(
        self,
        root: Path,
        directory: Path,
        root_fd: int,
        directory_fd: int,
    ) -> None:
        self.root = root
        self.directory = directory
        self.root_fd = root_fd
        self.directory_fd = directory_fd
        self.records: tuple[_LeasedAttachmentRecord, ...] = ()
        self.references = 1
        self.adopted = False
        self.lock = threading.Lock()


class InboundAttachmentLease:
    """Opaque reference to one private set of materialized native files."""

    __slots__ = ("__state", "__released")

    def __init__(self, state: _LeaseState) -> None:
        self.__state = state
        self.__released = False

    def retain(self) -> "InboundAttachmentLease":
        """Return an independent reference to the same immutable file set."""

        state = self.__state
        with state.lock:
            if self.__released or state.references <= 0:
                raise RuntimeError("attachment lease is no longer active")
            state.references += 1
        return InboundAttachmentLease(state)

    def adopt(self) -> None:
        """Transfer final-file ownership to the ordinary Agent attachment path."""

        state = self.__state
        with state.lock:
            if self.__released or state.references <= 0:
                raise RuntimeError("attachment lease is no longer active")
            state.adopted = True

    def release(self) -> None:
        """Release this reference and remove unadopted files after the last user."""

        state = self.__state
        with state.lock:
            if self.__released:
                return
            self.__released = True
            state.references -= 1
            last_reference = state.references == 0
            remove = last_reference and (not state.adopted or not state.records)
        if remove:
            _remove_lease_directory(state)
        elif last_reference:
            os.close(state.directory_fd)
            os.close(state.root_fd)


@dataclass(frozen=True, slots=True)
class MaterializedAttachmentBatch:
    """Agent-facing snapshots plus an opaque lease for independent consumers."""

    attachments: tuple[FileAttachment, ...]
    errors: tuple[str, ...]
    lease: InboundAttachmentLease
    display_errors: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _MaterializationFailure:
    reason: str
    display_error: str


class InboundAttachmentMaterializer:
    """Download native files once into an Avibe-owned private lease directory."""

    def __init__(
        self,
        *,
        effective_home: Path | str | None = None,
        attachments_root: Path | str | None = None,
    ) -> None:
        home = paths.get_vibe_remote_dir() if effective_home is None else effective_home
        self._home = Path(os.path.abspath(os.path.expanduser(os.fspath(home))))
        # The anchor is trusted and resolved once; every component below it is
        # Avibe-owned and stays under a per-component no-follow walk.
        anchor = paths.physical_home(self._home)
        if attachments_root is None:
            owned_parts: tuple[str, ...] = ("attachments", "im")
        else:
            declared_root = Path(os.path.abspath(os.path.expanduser(os.fspath(attachments_root))))
            relative_parts = _owned_parts_below(anchor, self._home, declared_root)
            if relative_parts is None:
                # A declared root outside the home is its own anchor, on the same
                # rule as the home: the path reaching it is operator config, and
                # Avibe only owns what it creates underneath.
                anchor = paths.physical_home(declared_root)
                owned_parts = ("im",)
            else:
                # Every component between the home and a declared root inside it
                # is Avibe-owned space, so all of them stay in the no-follow walk
                # instead of being resolved: a symlink planted at an intermediate
                # component such as ``<home>/custom`` must not redirect leases out
                # of the tree.
                owned_parts = (*relative_parts, "im")
        self._anchor = anchor
        self._owned_parts = owned_parts
        self._root = anchor.joinpath(*owned_parts)

    async def materialize(
        self,
        context: MessageContext,
        im_client: Any,
        *,
        max_bytes: int | None = None,
        timeout_seconds: int = 30,
        max_concurrency: int = 1,
        language: str = "en",
    ) -> MaterializedAttachmentBatch:
        root_fd = _open_or_create_private_directory(self._anchor, self._owned_parts)
        lease_id = secrets.token_hex(16)
        lease_dir = self._root / lease_id
        lease_fd: int | None = None
        try:
            os.mkdir(lease_id, mode=0o700, dir_fd=root_fd)
            lease_fd = os.open(lease_id, _directory_open_flags(), dir_fd=root_fd)
            _make_private_directory(lease_fd, "attachment lease directory")
        except BaseException:
            if lease_fd is not None:
                os.close(lease_fd)
            os.close(root_fd)
            raise
        state = _LeaseState(self._root, lease_dir, root_fd, lease_fd)
        lease = InboundAttachmentLease(state)

        candidates = tuple(
            attachment
            for attachment in (context.files or ())
            if isinstance(attachment, FileAttachment)
        )
        semaphore = asyncio.Semaphore(max(1, min(2, int(max_concurrency))))

        async def acquire(index: int, attachment: FileAttachment):
            async with semaphore:
                return await self._materialize_one(
                    context,
                    im_client,
                    lease_dir,
                    lease_fd,
                    index,
                    attachment,
                    max_bytes=max_bytes,
                    timeout_seconds=timeout_seconds,
                    language=language,
                )

        try:
            outcomes = await asyncio.gather(
                *(acquire(index, attachment) for index, attachment in enumerate(candidates))
            )
            _verify_lease_directory_entry(state)
        except BaseException:
            lease.release()
            raise

        attachments: list[FileAttachment] = []
        records: list[_LeasedAttachmentRecord] = []
        errors: list[str] = []
        display_errors: list[str] = []
        for outcome in outcomes:
            if isinstance(outcome, _MaterializationFailure):
                errors.append(outcome.reason)
                display_errors.append(outcome.display_error)
                continue
            attachment, record = outcome
            attachments.append(attachment)
            if record is not None:
                records.append(record)
        state.records = tuple(records)
        return MaterializedAttachmentBatch(
            tuple(attachments),
            tuple(errors),
            lease,
            tuple(display_errors),
        )

    async def _materialize_one(
        self,
        context: MessageContext,
        im_client: Any,
        lease_dir: Path,
        lease_fd: int,
        index: int,
        attachment: FileAttachment,
        *,
        max_bytes: int | None,
        timeout_seconds: int,
        language: str,
    ) -> tuple[FileAttachment, _LeasedAttachmentRecord | None] | _MaterializationFailure:
        if attachment.local_path and Path(attachment.local_path).is_file():
            size = attachment.size
            if size is None:
                try:
                    size = Path(attachment.local_path).stat().st_size
                except OSError:
                    size = None
            return (
                FileAttachment(
                    name=attachment.name,
                    mimetype=attachment.mimetype,
                    url=attachment.url,
                    content=attachment.content,
                    local_path=attachment.local_path,
                    size=size,
                ),
                None,
            )

        safe_name = _sanitize_filename(attachment.name)
        declared_size = _normalized_declared_size(attachment.size)
        if max_bytes is not None and declared_size is not None and declared_size > max_bytes:
            return _materialization_failure("file_too_large", attachment.name, language)
        final_name = f"{index:02d}-{safe_name}"
        partial_name = f"{final_name}.part"
        file_info = _download_info(context, attachment)
        published_names: set[str] = set()
        partial_fd: int | None = None
        materialized = False
        try:
            partial_fd = os.open(
                partial_name,
                _file_create_flags(),
                0o600,
                dir_fd=lease_fd,
            )
            stream_download = getattr(im_client, "download_file_to_path", None)
            if callable(stream_download):
                result = await stream_download(
                    file_info,
                    _descriptor_path(partial_fd),
                    **_supported_download_options(
                        stream_download,
                        max_bytes=max_bytes,
                        timeout_seconds=timeout_seconds,
                        target_fd=partial_fd,
                    ),
                )
                if not isinstance(result, FileDownloadResult):
                    result = FileDownloadResult(bool(result))
                if not result.success:
                    reason = (
                        "file_too_large"
                        if result.failure_reason == "file_too_large"
                        else "download_failed"
                    )
                    return _materialization_failure(reason, attachment.name, language)
            else:
                download = getattr(im_client, "download_file", None)
                if not callable(download):
                    return _materialization_failure("download_failed", attachment.name, language)
                content = await download(file_info)
                if not content:
                    return _materialization_failure("download_failed", attachment.name, language)
                os.ftruncate(partial_fd, 0)
                os.lseek(partial_fd, 0, os.SEEK_SET)
                _write_all(partial_fd, content)
            size = os.fstat(partial_fd).st_size
            if max_bytes is not None and size > max_bytes:
                return _materialization_failure("file_too_large", attachment.name, language)
            os.fchmod(partial_fd, 0o600)
            os.rename(
                partial_name,
                final_name,
                src_dir_fd=lease_fd,
                dst_dir_fd=lease_fd,
            )
            published_names.add(final_name)
            name, mimetype, final_name = _normalize_detected_media(
                attachment.name,
                attachment.mimetype,
                partial_fd,
                lease_fd,
                final_name,
            )
            published_names.add(final_name)
            os.fchmod(partial_fd, 0o600)
            published_info = os.fstat(partial_fd)
            published_sha256 = _sha256_fd(partial_fd)
            final_path = lease_dir / final_name
            snapshot = FileAttachment(
                name=name,
                mimetype=mimetype,
                local_path=str(final_path),
                size=size,
            )
            outcome = snapshot, _LeasedAttachmentRecord(
                name=name,
                mimetype=mimetype,
                declared_size=declared_size,
                size=size,
                path=final_path,
                device=published_info.st_dev,
                inode=published_info.st_ino,
                sha256=published_sha256,
            )
            materialized = True
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception:
            return _materialization_failure("download_failed", attachment.name, language)
        finally:
            _unlink_at_quietly(lease_fd, partial_name)
            if not materialized:
                for published_name in published_names:
                    _unlink_at_quietly(lease_fd, published_name)
            if partial_fd is not None:
                try:
                    os.close(partial_fd)
                except OSError:
                    pass


def leased_attachment_records(
    lease: InboundAttachmentLease,
) -> tuple[Path, int, tuple[_LeasedAttachmentRecord, ...]]:
    """Snapshot an active lease and duplicate its anchored directory descriptor."""

    if type(lease) is not InboundAttachmentLease:
        raise ValueError("invalid attachment lease")
    state = lease._InboundAttachmentLease__state
    released = lease._InboundAttachmentLease__released
    with state.lock:
        if released or state.references <= 0 or _LEASE_ID.fullmatch(state.directory.name) is None:
            raise ValueError("attachment lease is no longer active")
        records = state.records
        try:
            directory_fd = os.dup(state.directory_fd)
        except OSError as error:
            raise ValueError("attachment lease is no longer active") from error
    return state.root, directory_fd, records


def open_leased_attachment_record(
    directory_fd: int,
    record: _LeasedAttachmentRecord,
) -> tuple[int, os.stat_result]:
    """Open the exact materialized inode relative to its retained lease directory."""

    if type(record) is not _LeasedAttachmentRecord:
        raise ValueError("invalid leased attachment record")
    filename = record.path.name
    if not filename or Path(filename).name != filename:
        raise ValueError("invalid leased attachment record")
    try:
        descriptor = os.open(filename, _file_read_flags(), dir_fd=directory_fd)
    except OSError as error:
        raise ValueError("leased attachment is unavailable") from error
    try:
        info = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or (callable(getuid) and info.st_uid != getuid())
            or info.st_dev != record.device
            or info.st_ino != record.inode
            or info.st_size != record.size
        ):
            raise ValueError("leased attachment no longer matches its materialized inode")
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


def _download_info(context: MessageContext, attachment: FileAttachment) -> dict[str, Any]:
    info: dict[str, Any] = {
        "url": attachment.url,
        "name": attachment.name,
        "size": attachment.size,
        "platform": context.platform,
    }
    if attachment.url:
        info["url_private_download"] = attachment.url
    for key, value in getattr(attachment, "__dict__", {}).items():
        if key not in {"name", "mimetype", "url", "content", "local_path", "size"}:
            info[key] = value
    return info


def _supported_download_options(
    download: Any,
    *,
    max_bytes: int | None,
    timeout_seconds: int,
    target_fd: int,
) -> dict[str, Any]:
    """Pass optional bounds only to clients whose method contract accepts them."""

    try:
        parameters = inspect.signature(download).parameters
    except (TypeError, ValueError):
        return {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    options: dict[str, Any] = {}
    if accepts_kwargs or "max_bytes" in parameters:
        options["max_bytes"] = max_bytes
    if accepts_kwargs or "timeout_seconds" in parameters:
        options["timeout_seconds"] = timeout_seconds
    if accepts_kwargs or "target_fd" in parameters:
        options["target_fd"] = target_fd
    return options


def _materialization_failure(
    reason: str,
    attachment_name: str,
    language: str,
) -> _MaterializationFailure:
    key = "tooLarge" if reason == "file_too_large" else "failed"
    return _MaterializationFailure(
        reason,
        i18n_t(
            f"error.attachmentDownload.{key}",
            language,
            name=attachment_name,
        ),
    )


def _normalized_declared_size(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _directory_open_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("attachment leases require no-follow filesystem support")
    return (
        os.O_RDONLY
        | int(no_follow)
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _file_create_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("attachment leases require no-follow filesystem support")
    return (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | int(no_follow)
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _file_read_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("attachment leases require no-follow filesystem support")
    return (
        os.O_RDONLY
        | int(no_follow)
        | int(getattr(os, "O_NONBLOCK", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _descriptor_path(descriptor: int) -> str:
    root = "/dev/fd" if Path("/dev/fd").is_dir() else "/proc/self/fd"
    return f"{root}/{descriptor}"


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("attachment write made no progress")
        view = view[written:]


def _sha256_fd(descriptor: int) -> str:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
    return digest.hexdigest()


def _unlink_at_quietly(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _verify_lease_directory_entry(state: _LeaseState) -> None:
    expected = os.fstat(state.directory_fd)
    observed = os.stat(
        state.directory.name,
        dir_fd=state.root_fd,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_dev != expected.st_dev
        or observed.st_ino != expected.st_ino
    ):
        raise OSError("attachment lease directory changed during materialization")


def _remove_lease_directory(state: _LeaseState) -> None:
    try:
        try:
            names = os.listdir(state.directory_fd)
        except OSError:
            names = []
        for name in names:
            try:
                os.unlink(name, dir_fd=state.directory_fd)
            except OSError:
                pass
        try:
            _verify_lease_directory_entry(state)
            os.rmdir(state.directory.name, dir_fd=state.root_fd)
        except OSError:
            pass
    finally:
        os.close(state.directory_fd)
        os.close(state.root_fd)


def _owned_parts_below(
    anchor: Path,
    home: Path,
    declared_root: Path,
) -> tuple[str, ...] | None:
    """Components from the home down to ``declared_root``, or ``None`` if outside.

    A declared root under the home is accepted in either spelling: as written
    against the logical home, or already resolved against the physical one. The
    returned components are relative to the anchor either way, so the caller can
    keep every one of them inside the ``O_NOFOLLOW`` walk.
    """

    for base in (home, anchor):
        if declared_root.is_relative_to(base):
            return declared_root.relative_to(base).parts
    return None


def _open_or_create_private_directory(anchor: Path, owned_parts: tuple[str, ...]) -> int:
    """Create a private root while refusing symlinks in every owned component.

    ``anchor`` is the already-resolved trust anchor -- the Avibe home, or a
    declared root the operator put outside it: the operator owns the path that
    reaches it, so it is opened in one step and its own symlinked parents stay
    legal. ``owned_parts`` are every component Avibe creates underneath, and
    each one is opened with ``O_NOFOLLOW`` so a planted symlink cannot redirect
    a lease outside Avibe-owned storage.
    """

    if not anchor.is_absolute():
        raise RuntimeError("attachment lease root must be absolute")
    if not owned_parts or any(part in {"", ".", ".."} for part in owned_parts):
        raise RuntimeError("attachment lease root must name owned components")
    try:
        descriptor = os.open(anchor, _directory_open_flags())
    except FileNotFoundError:
        os.makedirs(anchor, mode=0o700, exist_ok=True)
        descriptor = os.open(anchor, _directory_open_flags())
    try:
        for component in owned_parts:
            try:
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        _make_private_directory(descriptor, "attachment lease root")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _make_private_directory(descriptor: int, label: str) -> None:
    info = os.fstat(descriptor)
    getuid = getattr(os, "getuid", None)
    if not stat.S_ISDIR(info.st_mode) or (
        callable(getuid) and info.st_uid != getuid()
    ):
        raise RuntimeError(f"{label} has unsafe ownership or type")
    os.fchmod(descriptor, 0o700)
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
        raise RuntimeError(f"{label} is not private")


def _sanitize_filename(name: object) -> str:
    raw = Path(str(name or "attachment")).name or "attachment"
    safe = _SAFE_FILENAME.sub("_", raw).replace("..", "_")
    suffix = Path(safe).suffix
    suffix_bytes = suffix.encode("utf-8", errors="ignore")
    if not suffix or len(suffix_bytes) >= _MAX_FILENAME_BYTES:
        return _truncate_utf8(safe, _MAX_FILENAME_BYTES) or "attachment"
    stem = safe[: -len(suffix)]
    stem_budget = _MAX_FILENAME_BYTES - len(suffix_bytes)
    stem = _truncate_utf8(stem, stem_budget)
    return f"{stem or _truncate_utf8('attachment', stem_budget)}{suffix}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="ignore")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _normalize_detected_media(
    name: str,
    mimetype: str,
    file_fd: int,
    lease_fd: int,
    filename: str,
) -> tuple[str, str, str]:
    os.lseek(file_fd, 0, os.SEEK_SET)
    sample = os.read(file_fd, AUDIO_SIGNATURE_SAMPLE_BYTES)
    detected = _detect_image_mime(sample) or detect_audio_mime_from_sample(sample)
    if detected is None:
        return _sanitize_filename(name), mimetype, filename
    detected_mime, suffix = detected
    display = f"{Path(_sanitize_filename(name)).stem}{suffix}"
    corrected = str(Path(filename).with_suffix(suffix))
    if corrected != filename:
        os.rename(
            filename,
            corrected,
            src_dir_fd=lease_fd,
            dst_dir_fd=lease_fd,
        )
    return display, detected_mime, corrected


def _detect_image_mime(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff", ".tiff"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None
