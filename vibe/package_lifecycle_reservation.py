"""The one OS reservation for package lifecycle and ordinary restart work.

The process registry is a narrowing cache for an already-held OS lock: its
token-owned membership starts after acquisition and ends before unlock. During
either boundary window, the OS lock remains authoritative for probe/acquire;
only matching-token cleanup may remove an entry, preserving any successor.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


LOCK_FILENAME = "package-lifecycle.lock"
_LOCK_BYTE_OFFSET = 4096
_IS_WINDOWS = os.name == "nt"


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class HolderType(_StringEnum):
    PACKAGE = "package"
    ORDINARY_RESTART = "ordinary_restart"


class ReservationLiveness(_StringEnum):
    FREE = "free"
    HELD = "held"


class BusyClassification(_StringEnum):
    PENDING_PUBLICATION = "busy_pending_publication"
    BUSY = "busy"


@dataclass(frozen=True)
class HolderInformation:
    """Diagnostic publication fields that never prove reservation ownership."""

    acquisition_id: str
    holder_type: HolderType
    pid: int
    acquired_at: str


@dataclass(frozen=True)
class LivenessProbeResult:
    liveness: ReservationLiveness
    publication: HolderInformation | None

    @property
    def publication_observed(self) -> bool:
        return self.publication is not None


@dataclass(frozen=True)
class BusyResult:
    classification: BusyClassification
    observations: int

    @property
    def retryable(self) -> bool:
        return True


_PROCESS_RESERVATIONS: dict[Path, object] = {}
_PROCESS_RESERVATIONS_LOCK = threading.Lock()
_PROCESS_RESERVATIONS_PID = os.getpid()


def _refresh_process_reservations() -> None:
    global _PROCESS_RESERVATIONS_PID
    pid = os.getpid()
    if pid != _PROCESS_RESERVATIONS_PID:
        _PROCESS_RESERVATIONS.clear()
        _PROCESS_RESERVATIONS_PID = pid


def _forget_process_reservation(key: Path, registration_token: object) -> None:
    with _PROCESS_RESERVATIONS_LOCK:
        _refresh_process_reservations()
        if _PROCESS_RESERVATIONS.get(key) is registration_token:
            del _PROCESS_RESERVATIONS[key]
        assert _PROCESS_RESERVATIONS.get(key) is not registration_token


def _try_os_lock(descriptor: int) -> bool:
    if _IS_WINDOWS:
        import msvcrt

        os.lseek(descriptor, _LOCK_BYTE_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise


def _unlock_os_lock(descriptor: int) -> None:
    if _IS_WINDOWS:
        import msvcrt

        os.lseek(descriptor, _LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to publish package lifecycle reservation")
        remaining = remaining[written:]


class PackageLifecycleReservation:
    """The supervisor's live reservation and diagnostic publication."""

    def __init__(
        self,
        *,
        descriptor: int,
        key: Path,
        path: Path,
        holder: HolderInformation,
        registration_token: object,
    ) -> None:
        self._descriptor: int | None = descriptor
        self._key = key
        self._registration_token = registration_token
        self.path = path
        self.holder = holder

    def duplicate_for_runner(self) -> int:
        """Return an inheritable POSIX OFD duplicate without transferring ownership."""
        if _IS_WINDOWS:
            raise OSError(errno.ENOTSUP, "Windows runners do not inherit reservation locks")
        descriptor = self._descriptor
        if descriptor is None:
            raise ValueError("package lifecycle reservation is closed")
        duplicate = os.dup(descriptor)
        try:
            os.set_inheritable(duplicate, True)
        except BaseException:
            os.close(duplicate)
            raise
        return duplicate

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        _forget_process_reservation(self._key, self._registration_token)
        try:
            _unlock_os_lock(descriptor)
        finally:
            os.close(descriptor)

    close = release

    def __enter__(self) -> PackageLifecycleReservation:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class PackageLifecycleReservationManager:
    """Acquire and observe the one runtime-scoped reservation without naming its owner."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.lock_path = self.runtime_dir / LOCK_FILENAME
        self._key = Path(os.path.abspath(self.lock_path))

    def acquire(self, holder_type: HolderType) -> PackageLifecycleReservation | None:
        """Try once; publish the new holder before doing any other owner work."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _PROCESS_RESERVATIONS_LOCK:
            _refresh_process_reservations()
            if self._key in _PROCESS_RESERVATIONS:
                return None

        descriptor: int | None = None
        locked = False
        registered = False
        registration_token = object()
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            if not _try_os_lock(descriptor):
                os.close(descriptor)
                descriptor = None
                return None
            locked = True
            with _PROCESS_RESERVATIONS_LOCK:
                _refresh_process_reservations()
                assert self._key not in _PROCESS_RESERVATIONS
                _PROCESS_RESERVATIONS[self._key] = registration_token
                registered = True
                assert _PROCESS_RESERVATIONS[self._key] is registration_token

            holder = HolderInformation(
                acquisition_id=uuid.uuid4().hex,
                holder_type=holder_type,
                pid=os.getpid(),
                acquired_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self._publish_holder(descriptor, holder)
            reservation = PackageLifecycleReservation(
                descriptor=descriptor,
                key=self._key,
                path=self.lock_path,
                holder=holder,
                registration_token=registration_token,
            )
            descriptor = None
            return reservation
        except BaseException:
            try:
                if registered:
                    _forget_process_reservation(self._key, registration_token)
            finally:
                if descriptor is not None:
                    try:
                        if locked:
                            _unlock_os_lock(descriptor)
                    finally:
                        os.close(descriptor)
            raise

    def probe(self) -> LivenessProbeResult:
        """Classify current lock liveness without trusting stale file bytes."""
        first = self._read_holder()
        with _PROCESS_RESERVATIONS_LOCK:
            _refresh_process_reservations()
            held_here = self._key in _PROCESS_RESERVATIONS
        if held_here:
            second = self._read_holder()
            return LivenessProbeResult(
                ReservationLiveness.HELD,
                first if first is not None and first == second else None,
            )
        if not self.lock_path.exists():
            return LivenessProbeResult(ReservationLiveness.FREE, None)

        descriptor = os.open(self.lock_path, os.O_RDWR)
        try:
            if _try_os_lock(descriptor):
                _unlock_os_lock(descriptor)
                return LivenessProbeResult(ReservationLiveness.FREE, None)
            second = self._read_holder()
            return LivenessProbeResult(
                ReservationLiveness.HELD,
                first if first is not None and first == second else None,
            )
        finally:
            os.close(descriptor)

    def classify_busy(
        self,
        *,
        rereads: int = 2,
        retry_delay: float = 0.01,
    ) -> BusyResult:
        """Bound publication rereads, then return owner-neutral contention."""
        observations = max(0, rereads) + 1
        saw_pending_publication = False
        for index in range(observations):
            probe = self.probe()
            saw_pending_publication |= probe.liveness is ReservationLiveness.HELD and not probe.publication_observed
            if index + 1 < observations:
                time.sleep(max(0.0, retry_delay))

        classification = (
            BusyClassification.PENDING_PUBLICATION
            if rereads <= 0 and saw_pending_publication
            else BusyClassification.BUSY
        )
        return BusyResult(classification, observations)

    def _publish_holder(self, descriptor: int, holder: HolderInformation) -> None:
        payload = {
            "acquisition_id": holder.acquisition_id,
            "holder_type": holder.holder_type.value,
            "pid": holder.pid,
            "acquired_at": holder.acquired_at,
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        assert len(encoded) < _LOCK_BYTE_OFFSET
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)

    def _read_holder(self) -> HolderInformation | None:
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags)
            expected_size = os.fstat(descriptor).st_size
            if expected_size <= 0 or expected_size >= _LOCK_BYTE_OFFSET:
                return None
            encoded = os.read(descriptor, _LOCK_BYTE_OFFSET)
            if len(encoded) != expected_size or os.fstat(descriptor).st_size != expected_size:
                return None
            payload = json.loads(encoded.decode("utf-8"))
            acquisition_id = payload["acquisition_id"]
            pid = payload["pid"]
            acquired_at = payload["acquired_at"]
            if not isinstance(acquisition_id, str) or not acquisition_id or not isinstance(pid, int) or pid <= 0:
                return None
            if not isinstance(acquired_at, str) or not acquired_at:
                return None
            return HolderInformation(
                acquisition_id=acquisition_id,
                holder_type=HolderType(payload["holder_type"]),
                pid=pid,
                acquired_at=acquired_at,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)


__all__ = [
    "BusyClassification",
    "BusyResult",
    "HolderInformation",
    "HolderType",
    "LivenessProbeResult",
    "LOCK_FILENAME",
    "PackageLifecycleReservation",
    "PackageLifecycleReservationManager",
    "ReservationLiveness",
]
