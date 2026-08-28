"""The one OS reservation for package lifecycle and ordinary restart work."""

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


_PROCESS_RESERVATIONS: set[Path] = set()
_PROCESS_RESERVATIONS_LOCK = threading.Lock()
_PROCESS_RESERVATIONS_PID = os.getpid()


def _refresh_process_reservations() -> None:
    global _PROCESS_RESERVATIONS_PID

    pid = os.getpid()
    if pid != _PROCESS_RESERVATIONS_PID:
        _PROCESS_RESERVATIONS.clear()
        _PROCESS_RESERVATIONS_PID = pid


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
    ) -> None:
        self._descriptor: int | None = descriptor
        self._key = key
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
        try:
            _unlock_os_lock(descriptor)
        finally:
            os.close(descriptor)
            with _PROCESS_RESERVATIONS_LOCK:
                _refresh_process_reservations()
                _PROCESS_RESERVATIONS.discard(self._key)

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
        try:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            if not _try_os_lock(descriptor):
                os.close(descriptor)
                descriptor = None
                return None
            locked = True
            with _PROCESS_RESERVATIONS_LOCK:
                _refresh_process_reservations()
                _PROCESS_RESERVATIONS.add(self._key)
                registered = True

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
            )
            descriptor = None
            return reservation
        except BaseException:
            if descriptor is not None:
                if locked:
                    _unlock_os_lock(descriptor)
                os.close(descriptor)
            if registered:
                self._forget_process_reservation()
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
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)

    def _read_holder(self) -> HolderInformation | None:
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
            acquisition_id = payload["acquisition_id"]
            pid = payload["pid"]
            acquired_at = payload["acquired_at"]
            if not isinstance(acquisition_id, str) or not acquisition_id:
                return None
            if not isinstance(pid, int) or pid <= 0:
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

    def _forget_process_reservation(self) -> None:
        with _PROCESS_RESERVATIONS_LOCK:
            _refresh_process_reservations()
            _PROCESS_RESERVATIONS.discard(self._key)


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
