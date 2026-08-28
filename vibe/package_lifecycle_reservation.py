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
    PACKAGE_TRANSACTION = "busy_package_transaction"
    ORDINARY_RESTART = "busy_restart"
    RESERVATION_PUBLICATION = "busy_reservation_publication"
    BUSY = "busy"


@dataclass(frozen=True)
class HolderInformation:
    acquisition_id: str
    holder_type: HolderType
    pid: int
    acquired_at: str


@dataclass(frozen=True)
class LivenessProbeResult:
    liveness: ReservationLiveness
    holder: HolderInformation | None

    @property
    def publication_consistent(self) -> bool:
        return self.liveness is ReservationLiveness.HELD and self.holder is not None


@dataclass(frozen=True)
class BusyResult:
    classification: BusyClassification
    holder: HolderInformation | None
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
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
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
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
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
    """A live descriptor plus the holder identity published for that acquisition."""

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

    def fileno(self) -> int:
        if self._descriptor is None:
            raise ValueError("package lifecycle reservation is closed")
        return self._descriptor

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
    """Acquire, probe, and classify the one runtime-scoped reservation."""

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
            _PROCESS_RESERVATIONS.add(self._key)

        descriptor: int | None = None
        locked = False
        try:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            if os.name == "nt" and os.fstat(descriptor).st_size == 0:
                _write_all(descriptor, b"\0")
            if not _try_os_lock(descriptor):
                os.close(descriptor)
                descriptor = None
                self._forget_process_reservation()
                return None
            locked = True

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
        expected_package_acquisition_id: str | None,
        rereads: int = 2,
        retry_delay: float = 0.01,
    ) -> BusyResult:
        """Return an owner-specific result only for a live consistent holder.

        The caller supplies the package acquisition identity from its own state;
        ``None`` means it established that no package identity exists. This module
        deliberately knows nothing about the source of that consistency fact.
        """

        observations = max(0, rereads) + 1
        for index in range(observations):
            probe = self.probe()
            holder = probe.holder
            if probe.publication_consistent and holder is not None:
                if (
                    holder.holder_type is HolderType.PACKAGE
                    and holder.acquisition_id == expected_package_acquisition_id
                ):
                    return BusyResult(BusyClassification.PACKAGE_TRANSACTION, holder, index + 1)
                if (
                    holder.holder_type is HolderType.ORDINARY_RESTART
                    and expected_package_acquisition_id is None
                ):
                    return BusyResult(BusyClassification.ORDINARY_RESTART, holder, index + 1)
            if index + 1 < observations:
                time.sleep(max(0.0, retry_delay))

        classification = (
            BusyClassification.RESERVATION_PUBLICATION
            if rereads <= 0
            else BusyClassification.BUSY
        )
        return BusyResult(classification, None, observations)

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
