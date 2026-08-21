"""Opaque lease primitives shared by inbound delivery and optional consumers."""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path


_LEASE_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class LeasedAttachmentRecord:
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
        self.records: tuple[LeasedAttachmentRecord, ...] = ()
        self.published = False
        self.references = 1
        self.adopted = False
        self.lock = threading.Lock()


class InboundAttachmentLease:
    """Opaque reference to one private set of materialized native files."""

    __slots__ = ("__state", "__released")

    def __init__(self, state: _LeaseState) -> None:
        self.__state = state
        self.__released = False

    @classmethod
    def create(
        cls,
        *,
        root: Path,
        directory: Path,
        root_fd: int,
        directory_fd: int,
    ) -> "InboundAttachmentLease":
        """Create the first reference owned by the shared materializer."""

        return cls(_LeaseState(root, directory, root_fd, directory_fd))

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

    def publish_records(
        self,
        records: tuple[LeasedAttachmentRecord, ...],
    ) -> None:
        """Publish the immutable record set after materialization verifies it."""

        state = self.__state
        with state.lock:
            if self.__released or state.references <= 0 or state.published:
                raise RuntimeError("attachment lease is no longer publishable")
            state.records = records
            state.published = True

    def verify_directory(self) -> None:
        """Verify that the retained directory still names the opened inode."""

        state = self.__state
        with state.lock:
            if self.__released or state.references <= 0:
                raise RuntimeError("attachment lease is no longer active")
            _verify_lease_directory_entry(state)

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


def leased_attachment_records(
    lease: InboundAttachmentLease,
) -> tuple[Path, int, tuple[LeasedAttachmentRecord, ...]]:
    """Snapshot an active lease and duplicate its anchored directory descriptor."""

    if type(lease) is not InboundAttachmentLease:
        raise ValueError("invalid attachment lease")
    state = lease._InboundAttachmentLease__state
    released = lease._InboundAttachmentLease__released
    with state.lock:
        if (
            released
            or state.references <= 0
            or _LEASE_ID.fullmatch(state.directory.name) is None
        ):
            raise ValueError("attachment lease is no longer active")
        records = state.records
        try:
            directory_fd = os.dup(state.directory_fd)
        except OSError as error:
            raise ValueError("attachment lease is no longer active") from error
    return state.root, directory_fd, records


def open_leased_attachment_record(
    directory_fd: int,
    record: LeasedAttachmentRecord,
) -> tuple[int, os.stat_result]:
    """Open the exact materialized inode relative to its retained directory."""

    if type(record) is not LeasedAttachmentRecord:
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
            raise ValueError(
                "leased attachment no longer matches its materialized inode"
            )
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


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
