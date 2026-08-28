"""MEMORY-INDEP-020 reservation/publication foundation evidence."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
from pathlib import Path

import pytest

from vibe import package_lifecycle_reservation as reservation_module
from vibe.package_lifecycle_reservation import (
    BusyClassification,
    HolderType,
    PackageLifecycleReservationManager,
    ReservationLiveness,
)


def _hold_reservation(runtime_dir: str, ready, release, result) -> None:
    manager = PackageLifecycleReservationManager(Path(runtime_dir))
    reservation = manager.acquire(HolderType.PACKAGE)
    if reservation is None:
        result.put({"error": "acquire_failed"})
        return
    try:
        result.put({"acquisition_id": reservation.holder.acquisition_id, "pid": reservation.holder.pid})
        ready.set()
        release.wait(10)
    finally:
        reservation.release()


def _hold_before_publication(runtime_dir: str, ready, proceed) -> None:
    manager = PackageLifecycleReservationManager(Path(runtime_dir))
    publish = manager._publish_holder

    def pause(descriptor, holder) -> None:
        ready.set()
        assert proceed.wait(10)
        publish(descriptor, holder)

    manager._publish_holder = pause
    reservation = manager.acquire(HolderType.PACKAGE)
    if reservation is not None:
        reservation.release()


def _hold_runner_duplicate(descriptor: int, ready, release) -> None:
    ready.set()
    release.wait(10)
    os.close(descriptor)


@pytest.mark.parametrize("holder_type", list(HolderType))
def test_memory_indep_020_publication_records_diagnostic_holder_types(tmp_path, holder_type) -> None:
    manager = PackageLifecycleReservationManager(tmp_path / holder_type.value)

    reservation = manager.acquire(holder_type)

    assert reservation is not None
    assert json.loads(manager.lock_path.read_text(encoding="utf-8")) == {
        "acquired_at": reservation.holder.acquired_at,
        "acquisition_id": reservation.holder.acquisition_id,
        "holder_type": holder_type.value,
        "pid": os.getpid(),
    }
    reservation.release()


def test_memory_indep_020_acquire_overwrites_metadata_and_release_is_reusable(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    manager.lock_path.write_text('{"acquisition_id":"stale"}' + ("x" * 200), encoding="utf-8")

    first = manager.acquire(HolderType.PACKAGE)
    assert first is not None
    first_id = first.holder.acquisition_id
    assert manager.acquire(HolderType.ORDINARY_RESTART) is None
    assert "stale" not in manager.lock_path.read_text(encoding="utf-8")
    first.release()
    first.release()

    second = manager.acquire(HolderType.ORDINARY_RESTART)
    assert second is not None
    assert second.holder.acquisition_id != first_id
    assert json.loads(manager.lock_path.read_text(encoding="utf-8"))["holder_type"] == "ordinary_restart"
    second.release()
    assert manager.probe().liveness is ReservationLiveness.FREE


@pytest.mark.parametrize("holder_type", list(HolderType))
def test_memory_indep_020_publication_never_drives_owner_classification(
    tmp_path,
    holder_type,
) -> None:
    manager = PackageLifecycleReservationManager(tmp_path / holder_type.value)
    reservation = manager.acquire(holder_type)
    assert reservation is not None
    publication_before_attempt = json.loads(manager.lock_path.read_text(encoding="utf-8"))

    probe = manager.probe()
    classified = manager.classify_busy(rereads=0)

    assert set(BusyClassification) == {
        BusyClassification.PENDING_PUBLICATION,
        BusyClassification.BUSY,
    }
    assert probe.liveness is ReservationLiveness.HELD
    assert probe.publication == reservation.holder
    assert classified.classification is BusyClassification.BUSY
    assert classified.observations == 1
    assert json.loads(manager.lock_path.read_text(encoding="utf-8")) == publication_before_attempt
    reservation.release()


@pytest.mark.parametrize("publication", [b"", b"{unreadable", b'{"acquisition_id":"stale"}'])
def test_memory_indep_020_inconsistent_live_publication_stays_retry_neutral(
    tmp_path,
    publication,
) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    reservation = manager.acquire(HolderType.PACKAGE)
    assert reservation is not None
    manager.lock_path.write_bytes(publication)

    immediate = manager.classify_busy(rereads=0)
    exhausted = manager.classify_busy(rereads=2, retry_delay=0)

    assert immediate.classification is BusyClassification.PENDING_PUBLICATION
    assert immediate.observations == 1
    assert exhausted.classification is BusyClassification.BUSY
    assert exhausted.observations == 3
    reservation.release()


def test_memory_indep_020_stale_free_bytes_are_not_a_live_holder(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    manager.lock_path.write_text(
        '{"acquisition_id":"released-acquisition","holder_type":"package",'
        '"pid":999999,"acquired_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    probe = manager.probe()
    classified = manager.classify_busy(rereads=0)

    assert probe.liveness is ReservationLiveness.FREE
    assert probe.publication is None
    assert probe.publication_observed is False
    assert classified.classification is BusyClassification.BUSY


def test_memory_indep_020_probe_ignores_pre_lock_acquisition_attempt(tmp_path, monkeypatch) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    real_try_os_lock = reservation_module._try_os_lock
    before_lock = threading.Event()
    allow_lock = threading.Event()
    acquirer_ident: int | None = None
    acquired = []

    def block_acquirer(descriptor: int) -> bool:
        if threading.get_ident() == acquirer_ident:
            before_lock.set()
            assert allow_lock.wait(10)
        return real_try_os_lock(descriptor)

    def acquire() -> None:
        nonlocal acquirer_ident
        acquirer_ident = threading.get_ident()
        acquired.append(manager.acquire(HolderType.PACKAGE))

    monkeypatch.setattr(reservation_module, "_try_os_lock", block_acquirer)
    thread = threading.Thread(target=acquire)
    thread.start()
    try:
        assert before_lock.wait(10)
        assert manager.probe().liveness is ReservationLiveness.FREE
    finally:
        allow_lock.set()
        thread.join(10)

    assert not thread.is_alive()
    assert len(acquired) == 1 and acquired[0] is not None
    assert manager.probe().liveness is ReservationLiveness.HELD
    acquired[0].release()


def test_memory_indep_020_recovery_turnover_never_names_stale_package_owner(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    manager.lock_path.write_text(
        '{"acquisition_id":"dead-package-a","holder_type":"package","pid":999999,"acquired_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    ready, proceed = context.Event(), context.Event()
    process = context.Process(
        target=_hold_before_publication,
        args=(str(tmp_path), ready, proceed),
    )
    process.start()
    try:
        assert ready.wait(10)
        observed = manager.probe()
        busy = manager.classify_busy(rereads=2, retry_delay=0)
        assert observed.publication is not None
        assert observed.publication.acquisition_id == "dead-package-a"
        assert busy.classification is BusyClassification.BUSY
        assert busy.observations == 3
    finally:
        proceed.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-file-description evidence")
def test_memory_indep_020_supervisor_retains_primary_with_runner_ofd_duplicate(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    reservation = manager.acquire(HolderType.ORDINARY_RESTART)
    assert reservation is not None
    runner_descriptor = reservation.duplicate_for_runner()
    assert os.get_inheritable(runner_descriptor) is True
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_runner_duplicate,
        args=(runner_descriptor, ready, release),
    )
    process.start()
    os.close(runner_descriptor)
    try:
        assert ready.wait(10)
        assert manager.acquire(HolderType.PACKAGE) is None
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    reservation.release()
    recovered = manager.acquire(HolderType.PACKAGE)
    assert recovered is not None
    recovered.release()


def test_memory_indep_020_windows_lock_byte_does_not_cover_publication(tmp_path, monkeypatch) -> None:
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2
        calls: list[tuple[int, int, int]] = []

        @classmethod
        def locking(cls, descriptor, mode, length) -> None:
            cls.calls.append((os.lseek(descriptor, 0, os.SEEK_CUR), mode, length))

    monkeypatch.setattr(reservation_module, "_IS_WINDOWS", True)
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    manager = PackageLifecycleReservationManager(tmp_path)

    reservation = manager.acquire(HolderType.PACKAGE)

    assert reservation is not None
    publication = manager.lock_path.read_bytes()
    assert len(publication) < reservation_module._LOCK_BYTE_OFFSET
    assert json.loads(publication)["acquisition_id"] == reservation.holder.acquisition_id
    assert PackageLifecycleReservationManager(tmp_path).probe().publication == reservation.holder
    with pytest.raises(OSError, match="Windows runners do not inherit"):
        reservation.duplicate_for_runner()
    reservation.release()
    assert FakeMsvcrt.calls == [
        (reservation_module._LOCK_BYTE_OFFSET, FakeMsvcrt.LK_NBLCK, 1),
        (reservation_module._LOCK_BYTE_OFFSET, FakeMsvcrt.LK_UNLCK, 1),
    ]


def test_memory_indep_020_publication_failure_releases_every_reservation_claim(
    tmp_path,
    monkeypatch,
) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(
            reservation_module.os,
            "fsync",
            lambda _descriptor: (_ for _ in ()).throw(OSError("disk unavailable")),
        )
        with pytest.raises(OSError, match="disk unavailable"):
            manager.acquire(HolderType.PACKAGE)

    recovered = manager.acquire(HolderType.PACKAGE)
    assert recovered is not None
    recovered.release()


def test_memory_indep_020_true_multiprocess_contention_and_release(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, release, result = context.Event(), context.Event(), context.Queue()
    process = context.Process(
        target=_hold_reservation,
        args=(str(tmp_path), ready, release, result),
    )
    process.start()
    try:
        assert ready.wait(10)
        child_holder = result.get(timeout=10)
        assert "error" not in child_holder
        manager = PackageLifecycleReservationManager(tmp_path)

        assert manager.acquire(HolderType.ORDINARY_RESTART) is None
        probe = manager.probe()
        busy = manager.classify_busy()
        assert probe.liveness is ReservationLiveness.HELD
        assert probe.publication is not None
        assert probe.publication.pid == child_holder["pid"]
        assert busy.classification is BusyClassification.BUSY
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    after_release = PackageLifecycleReservationManager(tmp_path).acquire(HolderType.ORDINARY_RESTART)
    assert after_release is not None
    after_release.release()
