"""One-use acknowledgement between the native helper and the UI service sharing its root.

HTTP can advance a registered pending digest, never create a permit or consume it. A
forwarding listener can relay an acknowledgement; it is not the machine or account peer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic_ns

from config import paths
from config.atomic_io import write_atomic
from core.private_fs import PrivatePathError, ensure_private_directory, is_private, windows_security
from storage.lock import MigrationFileLock
from vibe.personal_claim_client import ClaimError

ADMISSION_ROUTE = "/api/desktop/claim/v1/admit"
_TTL = 15_000_000_000  # monotonic nanoseconds, shared by processes on this machine
_HEX = re.compile(r"[a-f0-9]{64}")
_ORIGIN = re.compile(r"http://(?:127\.0\.0\.1|\[::1\])(?::([1-9][0-9]{0,4}))?")


class AdmissionUnavailable(Exception):
    pass


def literal_origin(value: object) -> bool:
    """Only the canonical serialization of an adopted native runtime origin."""
    m = isinstance(value, str) and _ORIGIN.fullmatch(value)
    return bool(m and (m[1] is None or (int(m[1]) <= 65535 and int(m[1]) not in (80, 1420))))


def _digest(challenge: str) -> str:
    return hashlib.sha256(challenge.encode("ascii")).hexdigest()


def _slot() -> Path:
    # The resolved Avibe home is the installation trust anchor (legacy home
    # symlinks are supported). Put the checked private directory immediately
    # beneath it, not below another, potentially redirected, directory.
    home = paths.physical_home(paths.get_vibe_remote_dir())
    return home / "desktop-claim-admission" / "slot.json"


def _private(path, info, *, directory=False, fd=None):
    return (
        (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
        and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and (directory or info.st_nlink == 1)
        and (os.name == "nt" or info.st_uid == os.geteuid())
        and is_private(path, info.st_mode, fd=fd)
    )


def _open_lock(path):
    if os.name == "nt":
        fd = windows_security().open_private_lock_file(path, allow_repair=False)
    else:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    return os.fdopen(fd, "r+", encoding="ascii")


@contextmanager
def _locked(slot: Path, *, register=False):
    directory = slot.parent
    lock = directory / "slot.lock"
    try:
        if register:
            ensure_private_directory(directory, repair=False)
        if not _private(directory, directory.lstat(), directory=True):
            raise AdmissionUnavailable()
        # HTTP on another root must not bootstrap even the lock file.
        if not register and not lock.exists():
            raise AdmissionUnavailable()

        def valid(handle):
            info = os.fstat(handle.fileno())
            return os.path.samestat(info, lock.lstat()) and _private(lock, info, fd=handle.fileno())

        with MigrationFileLock(lock, timeout_seconds=0.5, _handle_opener=_open_lock, _handle_validator=valid):
            yield
    except (OSError, ValueError, RecursionError):
        raise AdmissionUnavailable() from None


def _read(slot):
    try:
        before = slot.lstat()
    except FileNotFoundError:
        return None
    if not _private(slot, before):
        raise PrivatePathError("invalid admission slot")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(slot, flags), "rb") as f:
        info = os.fstat(f.fileno())
        if not os.path.samestat(before, info) or not _private(slot, info, fd=f.fileno()) or info.st_size > 512:
            raise AdmissionUnavailable()
        record = json.loads(f.read(513))
    if (
        not isinstance(record, dict)
        or set(record) != {"digest", "origin", "until", "phase"}
        or not isinstance(record["digest"], str)
        or not _HEX.fullmatch(record["digest"])
        or not literal_origin(record["origin"])
        or type(record["until"]) is not int
        or record["phase"] not in ("pending", "admitted")
    ):
        raise AdmissionUnavailable()
    return record


def _live(record):
    # A monotonic timestamp is not portable across boots. A pre-reboot permit
    # is either outside the short window or harmlessly delays recreation by at most one TTL.
    return record is not None and 0 < record["until"] - monotonic_ns() <= _TTL


@dataclass(frozen=True, repr=False)
class ClaimHome:
    path: Path
    identity: tuple[int, int]

    def check(self) -> None:
        # A resolved pathname can survive directory replacement; an inode can
        # survive a home migration. Neither alone is the admitted location.
        try:
            current = paths.physical_home(paths.get_vibe_remote_dir())
            info = self.path.lstat()
            if (
                current == self.path
                and stat.S_ISDIR(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and info.st_ino != 0
                and (info.st_dev, info.st_ino) == self.identity
            ):
                return
        except (OSError, ValueError, RuntimeError):
            pass
        raise ClaimError("local_binding_changed")


@dataclass(frozen=True, repr=False)
class HelperPermit:
    _slot: Path
    home: ClaimHome
    origin: str
    challenge: str

    @classmethod
    def register(cls, origin: str) -> HelperPermit:
        if not literal_origin(origin):
            raise AdmissionUnavailable()
        slot = _slot()
        with _locked(slot, register=True):
            root = slot.parent.parent.stat()
            home = ClaimHome(slot.parent.parent, (root.st_dev, root.st_ino))
            permit = cls(slot, home, origin, secrets.token_hex(32))
            if _live(_read(permit._slot)):
                raise AdmissionUnavailable()
            write_atomic(
                permit._slot,
                json.dumps(
                    dict(digest=_digest(permit.challenge), origin=origin, until=monotonic_ns() + _TTL, phase="pending")
                ),
            )
        return permit

    def _owns(self, record):
        return (
            record is not None
            and record["origin"] == self.origin
            and hmac.compare_digest(record["digest"], _digest(self.challenge))
        )

    def consume(self) -> bool:
        with _locked(self._slot):
            r = _read(self._slot)
            if not self._owns(r) or not _live(r) or r["phase"] != "admitted":
                return False
            self._slot.unlink()
            return True

    def close(self) -> None:
        with _locked(self._slot):
            if self._owns(_read(self._slot)):
                self._slot.unlink()


def acknowledge(origin: str, challenge: object) -> bool:
    if not literal_origin(origin) or not isinstance(challenge, str) or not _HEX.fullmatch(challenge):
        return False
    slot = _slot()
    try:
        with _locked(slot):
            r = _read(slot)
            if (
                not _live(r)
                or r["phase"] != "pending"
                or origin != r["origin"]
                or not hmac.compare_digest(r["digest"], _digest(challenge))
            ):
                return False
            write_atomic(slot, json.dumps({**r, "phase": "admitted"}))
            return True
    except AdmissionUnavailable:
        return False
