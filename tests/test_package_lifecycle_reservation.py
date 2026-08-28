"""MEMORY-INDEP-020 reservation/publication foundation evidence."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

from vibe import package_lifecycle_reservation as reservation_module
from vibe.package_lifecycle_reservation import (
    BusyClassification,
    HolderCorrelationEvidence,
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


def _hold_inherited_descriptor(descriptor: int, ready, release) -> None:
    ready.set()
    release.wait(10)
    os.close(descriptor)


def _correlation(reservation) -> HolderCorrelationEvidence:
    return HolderCorrelationEvidence(reservation.holder.acquisition_id, reservation.holder.holder_type)


@pytest.mark.parametrize("holder_type", list(HolderType))
def test_memory_indep_020_holder_publication_names_each_owner(tmp_path, holder_type) -> None:
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


def test_memory_indep_020_late_contender_uses_consistency_not_id_change(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    package = manager.acquire(HolderType.PACKAGE)
    assert package is not None
    publication_before_attempt = json.loads(manager.lock_path.read_text(encoding="utf-8"))

    classified = manager.classify_busy(
        correlation=_correlation(package),
    )

    assert classified.classification is BusyClassification.PACKAGE_TRANSACTION
    assert classified.holder == package.holder
    assert classified.observations == 1
    assert json.loads(manager.lock_path.read_text(encoding="utf-8")) == publication_before_attempt
    mismatched = manager.classify_busy(
        correlation=HolderCorrelationEvidence("different-live-acquisition", HolderType.PACKAGE),
        rereads=0,
    )
    assert mismatched.classification is BusyClassification.BUSY
    assert mismatched.holder is None
    package.release()

    ordinary = manager.acquire(HolderType.ORDINARY_RESTART)
    assert ordinary is not None
    uncorrelated = manager.classify_busy()
    assert uncorrelated.classification is BusyClassification.BUSY
    restart_busy = manager.classify_busy(correlation=_correlation(ordinary))
    assert restart_busy.classification is BusyClassification.ORDINARY_RESTART
    assert restart_busy.holder == ordinary.holder
    ordinary.release()


@pytest.mark.parametrize("publication", [b"", b"{unreadable", b'{"acquisition_id":"stale"}'])
def test_memory_indep_020_inconsistent_live_publication_stays_retry_neutral(
    tmp_path,
    publication,
) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    reservation = manager.acquire(HolderType.PACKAGE)
    assert reservation is not None
    manager.lock_path.write_bytes(publication)
    correlation = _correlation(reservation)

    immediate = manager.classify_busy(
        correlation=correlation,
        rereads=0,
    )
    exhausted = manager.classify_busy(
        correlation=correlation,
        rereads=2,
        retry_delay=0,
    )

    assert immediate.classification is BusyClassification.RESERVATION_PUBLICATION
    assert immediate.holder is None
    assert immediate.observations == 1
    assert exhausted.classification is BusyClassification.BUSY
    assert exhausted.holder is None
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

    assert probe.liveness is ReservationLiveness.FREE
    assert probe.holder is None
    assert probe.publication_consistent is False


def test_memory_indep_020_holder_turnover_never_guesses_from_stale_bytes(tmp_path) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    manager.lock_path.write_text(
        '{"acquisition_id":"previous-restart","holder_type":"ordinary_restart",'
        '"pid":999999,"acquired_at":"2026-01-01T00:00:00Z"}',
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
        busy = manager.classify_busy(rereads=2, retry_delay=0)
        assert busy.classification is BusyClassification.BUSY
        assert busy.holder is None
        assert busy.observations == 3
    finally:
        proceed.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-file-description evidence")
def test_memory_indep_020_parent_handoff_closes_without_unlock(tmp_path, monkeypatch) -> None:
    manager = PackageLifecycleReservationManager(tmp_path)
    reservation = manager.acquire(HolderType.ORDINARY_RESTART)
    assert reservation is not None
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_inherited_descriptor,
        args=(reservation.fileno(), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with monkeypatch.context() as patch:
            patch.setattr(
                reservation_module,
                "_unlock_os_lock",
                lambda _descriptor: (_ for _ in ()).throw(AssertionError("unexpected unlock")),
            )
            reservation.close_parent_duplicate()
            assert manager.acquire(HolderType.PACKAGE) is None
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
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
    assert PackageLifecycleReservationManager(tmp_path).probe().holder == reservation.holder
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
        busy = manager.classify_busy(
            correlation=HolderCorrelationEvidence(child_holder["acquisition_id"], HolderType.PACKAGE),
        )
        assert probe.liveness is ReservationLiveness.HELD
        assert probe.holder is not None
        assert probe.holder.pid == child_holder["pid"]
        assert busy.classification is BusyClassification.PACKAGE_TRANSACTION
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
