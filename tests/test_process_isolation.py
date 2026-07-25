from __future__ import annotations

import asyncio
import logging
import os
import signal
from types import SimpleNamespace

import pytest

from core.process_isolation import (
    KILL_SIGNAL,
    ProcessIdentity,
    fingerprint_process_command,
    inspect_process_identity,
    isolated_subprocess_kwargs,
    persist_process_identity,
    signal_process_tree,
    terminate_process_group_by_pgid,
    terminate_process_tree_by_pid,
)


def test_isolated_subprocess_kwargs_start_new_session_on_posix() -> None:
    if os.name == "nt":
        assert "creationflags" in isolated_subprocess_kwargs()
    else:
        assert isolated_subprocess_kwargs() == {"start_new_session": True}


def test_process_identity_accepts_empty_non_executable_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    process = SimpleNamespace(
        create_time=lambda: 123.0,
        cmdline=lambda: ["python3", "wait.py", ""],
    )
    monkeypatch.setattr("core.process_isolation.psutil.Process", lambda _pid: process)

    identity = inspect_process_identity(12345)

    assert identity is not None
    assert identity.cmdline == ("python3", "wait.py", "")
    assert fingerprint_process_command(("ab", "c")) != fingerprint_process_command(("a", "bc"))
    assert fingerprint_process_command(("python3", "")) != fingerprint_process_command(("python3",))


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
    identity = ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
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
            expected_identity=persist_process_identity(identity),
        )
        is False
    )


def test_terminate_process_tree_by_pid_refuses_service_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group guard is POSIX-specific")

    identity = ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
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
            expected_identity=persist_process_identity(identity),
        )
        is False
    )


def test_terminate_process_tree_by_pid_escalates_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    identity = ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
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
            expected_identity=persist_process_identity(identity),
        )
        is True
    )
    assert signals == [signal.SIGTERM, KILL_SIGNAL]


def test_terminate_process_tree_by_pid_does_not_signal_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    expected_identity = persist_process_identity(
        ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
    )
    reused_identity = ProcessIdentity(pid=12345, create_time=456.0, cmdline=("python", "wait.py"))
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


def test_terminate_process_tree_by_pid_refuses_changed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_identity = persist_process_identity(
        ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
    )
    changed_identity = ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "other.py"))
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), changed_identity),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda _pid: pytest.fail("a changed process command must not have its process group inspected"),
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

    identity = ProcessIdentity(pid=12345, create_time=123.0, cmdline=("python", "wait.py"))
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
            expected_identity=persist_process_identity(identity),
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
        )
        is True
    )
    assert signals == [signal.SIGTERM, KILL_SIGNAL]


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
