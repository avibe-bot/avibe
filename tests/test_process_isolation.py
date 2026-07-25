from __future__ import annotations

import asyncio
import logging
import os
import signal
from types import SimpleNamespace

import pytest

from core.process_isolation import (
    KILL_SIGNAL,
    PROCESS_IDENTITY_ENV,
    PersistedProcessIdentity,
    ProcessIdentity,
    capture_spawned_process_identity,
    fingerprint_process_marker,
    inspect_process_identity,
    isolated_subprocess_kwargs,
    new_process_identity_marker,
    process_group_identity_status,
    process_identity_matches,
    process_identity_subprocess_env,
    signal_process_tree,
    terminate_process_group_by_pgid,
    terminate_process_tree_by_pid,
)

TEST_MARKER = "test-worker-marker"
TEST_FINGERPRINT = fingerprint_process_marker(TEST_MARKER)


def _live_identity(
    *,
    pid: int = 12345,
    create_time: float = 123.0,
    worker_fingerprint: str | None = TEST_FINGERPRINT,
) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=worker_fingerprint,
    )


def _persisted_identity(
    *,
    pid: int = 12345,
    create_time: float = 123.0,
    worker_fingerprint: str = TEST_FINGERPRINT,
) -> PersistedProcessIdentity:
    return PersistedProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=worker_fingerprint,
    )


def test_isolated_subprocess_kwargs_start_new_session_on_posix() -> None:
    if os.name == "nt":
        assert "creationflags" in isolated_subprocess_kwargs()
    else:
        assert isolated_subprocess_kwargs() == {"start_new_session": True}


def test_process_identity_reads_inherited_worker_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    process = SimpleNamespace(
        create_time=lambda: 123.0,
        environ=lambda: {PROCESS_IDENTITY_ENV: TEST_MARKER},
    )
    monkeypatch.setattr("core.process_isolation.psutil.Process", lambda _pid: process)

    identity = inspect_process_identity(12345)

    assert identity == _live_identity()


def test_process_identity_survives_exec_transition() -> None:
    if os.name == "nt":
        pytest.skip("exec transition is POSIX-specific")

    async def _run() -> None:
        marker = new_process_identity_marker()
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            "sleep 0.1; exec python3 -c 'import time; time.sleep(30)'",
            env=process_identity_subprocess_env(marker),
            **isolated_subprocess_kwargs(),
        )
        try:
            expected = capture_spawned_process_identity(process.pid, marker)
            assert expected is not None
            await asyncio.sleep(0.2)
            live = inspect_process_identity(process.pid)
            assert live is not None
            assert process_identity_matches(expected, live)
        finally:
            signal_process_tree(process, KILL_SIGNAL, logging.getLogger(__name__), "test process")
            await process.wait()

    asyncio.run(_run())


def test_signal_process_tree_refuses_own_process_group_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group guard is POSIX-specific")

    sent: list[tuple[int, int]] = []
    process = SimpleNamespace(
        pid=12345,
        terminate=lambda: sent.append(("terminate", 0)),  # type: ignore[list-item]
        kill=lambda: sent.append(("kill", 0)),  # type: ignore[list-item]
    )
    monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    signal_process_tree(process, signal.SIGTERM, logging.getLogger(__name__), "test process")

    assert sent == [("terminate", 0)]


def test_terminate_process_tree_by_pid_refuses_current_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getpid", lambda: 12345)
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: pytest.fail("the current process must not be inspected for termination"),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=_persisted_identity(),
        )
        is False
    )


def test_terminate_process_tree_by_pid_refuses_service_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group guard is POSIX-specific")

    identity = _live_identity()
    process = SimpleNamespace(pid=12345)
    monkeypatch.setattr("core.process_isolation._open_process_identity", lambda _pid: (process, identity))
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)
    monkeypatch.setattr(os, "getpgrp", lambda: 12345)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: pytest.fail("the service process group must not be signaled"),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=_persisted_identity(),
        )
        is False
    )


def test_terminate_process_tree_by_pid_escalates_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    identity = _live_identity()
    signals: list[int] = []
    waits = iter([False, True])

    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), identity),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)
    monkeypatch.setattr(
        "core.process_isolation._safe_signal_known_process_group",
        lambda _pgid, _pid, sig, _logger, _label: signals.append(sig) or True,
    )
    monkeypatch.setattr(
        "core.process_isolation._wait_for_process_group_exit",
        lambda *_args, **_kwargs: next(waits),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=_persisted_identity(),
        )
        is True
    )
    assert signals == [signal.SIGTERM, KILL_SIGNAL]


def test_terminate_process_tree_by_pid_does_not_signal_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    expected_identity = _persisted_identity()
    reused_identity = _live_identity(create_time=456.0)
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), reused_identity),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda _pid: pytest.fail("a reused pid must not have its process group inspected"),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=expected_identity,
        )
        is True
    )


def test_terminate_process_tree_by_pid_refuses_changed_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_identity = _persisted_identity()
    changed_identity = _live_identity(
        worker_fingerprint=fingerprint_process_marker("other-worker-marker")
    )
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), changed_identity),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda _pid: pytest.fail("a changed worker marker must not have its process group inspected"),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=expected_identity,
        )
        is False
    )


def test_terminate_process_tree_by_pid_refuses_non_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    identity = _live_identity()
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), identity),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 54321)
    monkeypatch.setattr(
        "core.process_isolation._safe_signal_known_process_group",
        lambda *_args: pytest.fail("a non-leader process group must not be signaled"),
    )

    assert (
        terminate_process_tree_by_pid(
            12345,
            logging.getLogger(__name__),
            "test process",
            expected_identity=_persisted_identity(),
        )
        is False
    )


def test_terminate_process_group_by_pgid_recovers_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    signals: list[int] = []
    waits = iter([False, True])
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr("core.process_isolation._process_group_exists", lambda *_args: True)
    monkeypatch.setattr(
        "core.process_isolation.process_group_matches_identity",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "core.process_isolation._safe_signal_known_process_group",
        lambda _pgid, _pid, sig, _logger, _label: signals.append(sig) or True,
    )
    monkeypatch.setattr(
        "core.process_isolation._wait_for_process_group_exit",
        lambda *_args, **_kwargs: next(waits),
    )

    assert (
        terminate_process_group_by_pgid(
            12345,
            logging.getLogger(__name__),
            "test process group",
            expected_identity=_persisted_identity(),
        )
        is True
    )
    assert signals == [signal.SIGTERM, KILL_SIGNAL]


def test_terminate_process_group_by_pgid_refuses_unverified_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    monkeypatch.setattr(os, "getpgrp", lambda: 99999)
    monkeypatch.setattr("core.process_isolation._process_group_exists", lambda *_args: True)
    monkeypatch.setattr(
        "core.process_isolation.process_group_matches_identity",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        "core.process_isolation._safe_signal_known_process_group",
        lambda *_args: pytest.fail("an unverified process group must not be signaled"),
    )

    assert (
        terminate_process_group_by_pgid(
            12345,
            logging.getLogger(__name__),
            "test process group",
            expected_identity=_persisted_identity(),
        )
        is False
    )


@pytest.mark.parametrize(
    ("live_fingerprint", "expected_status"),
    [
        (TEST_FINGERPRINT, "match"),
        (fingerprint_process_marker("other-worker-marker"), "mismatch"),
    ],
)
def test_process_group_identity_status_checks_inherited_worker_marker(
    monkeypatch: pytest.MonkeyPatch,
    live_fingerprint: str,
    expected_status: str,
) -> None:
    if os.name == "nt":
        pytest.skip("process group identity is POSIX-specific")

    process = SimpleNamespace(info={"pid": 54321})
    monkeypatch.setattr(
        "core.process_isolation.psutil.process_iter",
        lambda _attrs: [process],
    )
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)
    monkeypatch.setattr(
        "core.process_isolation.inspect_process_identity",
        lambda pid: _live_identity(
            pid=pid,
            create_time=124.0,
            worker_fingerprint=live_fingerprint,
        ),
    )

    assert (
        process_group_identity_status(
            12345,
            _persisted_identity(),
            logging.getLogger(__name__),
            "test process group",
        )
        == expected_status
    )


def test_process_group_identity_status_fails_closed_for_unverified_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("process group identity is POSIX-specific")

    processes = [
        SimpleNamespace(info={"pid": 54321}),
        SimpleNamespace(info={"pid": 54322}),
    ]
    monkeypatch.setattr(
        "core.process_isolation.psutil.process_iter",
        lambda _attrs: processes,
    )
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)

    def inspect(pid: int) -> ProcessIdentity | None:
        if pid == 54321:
            return _live_identity(
                pid=pid,
                create_time=124.0,
                worker_fingerprint=fingerprint_process_marker("other-worker-marker"),
            )
        return None

    monkeypatch.setattr("core.process_isolation.inspect_process_identity", inspect)

    assert (
        process_group_identity_status(
            12345,
            _persisted_identity(),
            logging.getLogger(__name__),
            "test process group",
        )
        == "unknown"
    )


def test_asyncio_subprocess_is_spawned_outside_parent_process_group_on_posix() -> None:
    if os.name == "nt":
        pytest.skip("POSIX process-group assertion")

    async def _run() -> None:
        process = await asyncio.create_subprocess_exec(
            "python3",
            "-c",
            "import time; time.sleep(30)",
            **isolated_subprocess_kwargs(),
        )
        try:
            assert os.getpgid(process.pid) != os.getpgrp()
        finally:
            signal_process_tree(process, KILL_SIGNAL, logging.getLogger(__name__), "test process")
            await process.wait()

    asyncio.run(_run())
