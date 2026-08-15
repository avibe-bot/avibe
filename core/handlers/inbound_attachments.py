"""Shared, leased materialization for native inbound attachments."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import secrets
import shutil
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


class _LeaseState:
    def __init__(self, root: Path, directory: Path) -> None:
        self.root = root
        self.directory = directory
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
            remove = state.references == 0 and (not state.adopted or not state.records)
        if remove:
            shutil.rmtree(state.directory, ignore_errors=True)


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
        self._root = (
            self._home / "attachments" / "im"
            if attachments_root is None
            else Path(attachments_root) / "im"
        )

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
        root_fd = _open_or_create_private_directory(self._root)
        lease_id = secrets.token_hex(16)
        lease_dir = self._root / lease_id
        try:
            os.mkdir(lease_id, mode=0o700, dir_fd=root_fd)
            lease_fd = os.open(lease_id, _directory_open_flags(), dir_fd=root_fd)
            try:
                _make_private_directory(lease_fd, "attachment lease directory")
            finally:
                os.close(lease_fd)
        finally:
            os.close(root_fd)
        state = _LeaseState(self._root, lease_dir)
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
        final_path = lease_dir / f"{index:02d}-{safe_name}"
        partial_path = lease_dir / f"{index:02d}-{safe_name}.part"
        file_info = _download_info(context, attachment)
        published_paths: set[Path] = set()
        materialized = False
        try:
            stream_download = getattr(im_client, "download_file_to_path", None)
            if callable(stream_download):
                result = await stream_download(
                    file_info,
                    str(partial_path),
                    **_supported_download_options(
                        stream_download,
                        max_bytes=max_bytes,
                        timeout_seconds=timeout_seconds,
                    ),
                )
                if not isinstance(result, FileDownloadResult):
                    result = FileDownloadResult(bool(result))
                if not result.success or not partial_path.is_file():
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
                partial_path.write_bytes(content)
            size = partial_path.stat().st_size
            if max_bytes is not None and size > max_bytes:
                return _materialization_failure("file_too_large", attachment.name, language)
            partial_path.chmod(0o600)
            os.replace(partial_path, final_path)
            published_paths.add(final_path)
            name, mimetype, final_path = _normalize_detected_media(
                attachment.name,
                attachment.mimetype,
                final_path,
            )
            published_paths.add(final_path)
            final_path.chmod(0o600)
            snapshot = FileAttachment(
                name=name,
                mimetype=mimetype,
                local_path=str(final_path),
                size=size,
            )
            outcome = snapshot, _LeasedAttachmentRecord(
                name=name,
                mimetype=mimetype,
                declared_size=attachment.size,
                size=size,
                path=final_path,
            )
            materialized = True
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception:
            return _materialization_failure("download_failed", attachment.name, language)
        finally:
            partial_path.unlink(missing_ok=True)
            if not materialized:
                for published_path in published_paths:
                    published_path.unlink(missing_ok=True)


def leased_attachment_records(
    lease: InboundAttachmentLease,
) -> tuple[Path, tuple[_LeasedAttachmentRecord, ...]]:
    """Resolve an active exact-type lease for the Memory boundary."""

    if type(lease) is not InboundAttachmentLease:
        raise ValueError("invalid attachment lease")
    state = lease._InboundAttachmentLease__state
    released = lease._InboundAttachmentLease__released
    with state.lock:
        if released or state.references <= 0 or _LEASE_ID.fullmatch(state.directory.name) is None:
            raise ValueError("attachment lease is no longer active")
        records = state.records
    return state.root, records


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


def _open_or_create_private_directory(directory: Path) -> int:
    """Create a private root while refusing symlinks in every path component."""

    if not directory.is_absolute():
        raise RuntimeError("attachment lease root must be absolute")
    descriptor = os.open(directory.anchor, _directory_open_flags())
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


def _normalize_detected_media(name: str, mimetype: str, path: Path) -> tuple[str, str, Path]:
    with path.open("rb") as file_obj:
        sample = file_obj.read(AUDIO_SIGNATURE_SAMPLE_BYTES)
    detected = _detect_image_mime(sample) or detect_audio_mime_from_sample(sample)
    if detected is None:
        return _sanitize_filename(name), mimetype, path
    detected_mime, suffix = detected
    display = f"{Path(_sanitize_filename(name)).stem}{suffix}"
    corrected = path.with_suffix(suffix)
    if corrected != path:
        os.replace(path, corrected)
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
