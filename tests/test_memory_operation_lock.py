from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from config.memory_operation_lock import (
    MemoryOperationBusy,
    MemoryOperationLease,
    memory_operation_lock_path,
)


def _try_lease(home: Path, connection) -> None:
    lease = MemoryOperationLease(home)
    try:
        lease.acquire()
    except MemoryOperationBusy:
        connection.send("busy")
    else:
        connection.send("acquired")
        lease.release()
    finally:
        connection.close()


def _acquire_then_crash(home: Path, acquired) -> None:
    MemoryOperationLease(home).acquire()
    acquired.set()
    os._exit(23)


def test_memory_operation_lease_is_non_reentrant_and_release_is_idempotent(
    tmp_path: Path,
) -> None:
    first = MemoryOperationLease(tmp_path)
    second = MemoryOperationLease(tmp_path)

    first.acquire()
    descriptor = first._descriptor
    assert descriptor is not None
    assert os.get_inheritable(descriptor) is False
    with pytest.raises(MemoryOperationBusy):
        second.acquire()

    first.release()
    first.release()
    assert memory_operation_lock_path(tmp_path).is_file()

    second.acquire()
    second.release()


def test_memory_operation_lease_rejects_another_process(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    process = context.Process(target=_try_lease, args=(tmp_path, child))

    try:
        process.start()
        child.close()
        assert parent.recv() == "busy"
        process.join(10)
        assert process.exitcode == 0
    finally:
        lease.release()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_memory_operation_lease_is_released_when_owner_crashes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    process = context.Process(target=_acquire_then_crash, args=(tmp_path, acquired))

    process.start()
    assert acquired.wait(10)
    process.join(10)
    assert process.exitcode == 23

    lease = MemoryOperationLease(tmp_path)
    lease.acquire()
    lease.release()
