from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from core import watch_worker
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
    process_group_exists,
    process_group_identity_status,
    process_identity_matches,
    process_identity_subprocess_env,
    probe_process_liveness,
    reap_orphaned_process_tree,
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


def test_probe_tells_an_empty_pid_apart_from_one_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three answers must stay three, because callers act oppositely on two of them.

    ``inspect_process_identity`` folds "gone" and "could not look" into ``None``,
    which is right for deciding whether to signal and wrong for deciding whether to
    DISCARD the record that names the process: only an empty pid means there is
    nothing left to keep a handle for.
    """

    assert probe_process_liveness(os.getpid()) == "alive"
    assert probe_process_liveness(0) == "gone"
    assert probe_process_liveness(-1) == "gone"

    def _raise(exc: BaseException):
        def _factory(_pid: int):
            raise exc

        return _factory

    monkeypatch.setattr(
        "core.process_isolation.psutil.Process", _raise(psutil.NoSuchProcess(12345))
    )
    assert probe_process_liveness(12345) == "gone"

    monkeypatch.setattr(
        "core.process_isolation.psutil.Process", _raise(ProcessLookupError())
    )
    assert probe_process_liveness(12345) == "gone"

    # A process that is there but unreadable: an exhausted fd table on the way into
    # ``/proc``, or a platform refusing ``create_time``.
    monkeypatch.setattr(
        "core.process_isolation.psutil.Process", _raise(OSError(24, "Too many open files"))
    )
    assert probe_process_liveness(12345) == "unknown"

    monkeypatch.setattr(
        "core.process_isolation.psutil.Process", _raise(psutil.AccessDenied(12345))
    )
    assert probe_process_liveness(12345) == "unknown"


_LEADER_THAT_LEAVES_A_CHILD = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
    "sys.stdout.write(f'{child.pid}\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(0.5)\n"
    "os._exit(0)\n"
)


def test_reaping_an_orphan_follows_the_group_its_leader_left_behind() -> None:
    """An empty pid is not an empty tree, so it cannot be where the reap stops.

    A supervisor started with ``start_new_session`` leads the group ``pgid == pid``,
    and on POSIX that group outlives it: kill the supervisor -- an OOM kill, a fault
    in its own code -- and the backup or migration under it keeps running in a group
    nothing else names. A recovery pass that reads the free pid as proof the tree was
    reaped then drops the only record of it, and the survivor runs to completion
    unowned while the next fire starts a second one beside it.
    """

    if os.name == "nt":
        pytest.skip("process groups outliving their leader is POSIX-specific")

    marker = new_process_identity_marker()
    leader = subprocess.Popen(  # noqa: S603 - fixed argv, test-owned
        [os.path.abspath(sys.executable), "-c", _LEADER_THAT_LEAVES_A_CHILD],
        stdout=subprocess.PIPE,
        text=True,
        env=process_identity_subprocess_env(marker),
        **isolated_subprocess_kwargs(),
    )
    child_pid: int | None = None
    try:
        identity = capture_spawned_process_identity(leader.pid, marker)
        assert identity is not None
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().strip())
        assert leader.wait(timeout=15) == 0, "the leader did not exit on its own"

        # The state under test: leader reaped, its group still occupied by the work.
        assert probe_process_liveness(leader.pid) == "gone"
        assert os.getpgid(child_pid) == leader.pid
        assert process_group_exists(leader.pid, logging.getLogger(__name__), "test group")

        outcome = reap_orphaned_process_tree(
            logging.getLogger(__name__),
            "test orphan",
            expected_identity=identity,
        )

        assert outcome == "reaped", (
            "the surviving child was left running and its identity was about to be "
            f"discarded as spent: {outcome}"
        )
        assert not psutil.pid_exists(child_pid) or _is_reaped(child_pid), (
            "the child outlived a reap that reported success"
        )
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, KILL_SIGNAL)
        with suppress(Exception):
            leader.kill()
        with suppress(Exception):
            leader.wait(timeout=5)
        if leader.stdout is not None:
            leader.stdout.close()


def _is_reaped(pid: int) -> bool:
    """Whether a pid that still exists is only a zombie awaiting its parent."""
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def test_reaping_an_orphan_keeps_a_group_it_cannot_identify() -> None:
    """A group it cannot vouch for is neither killed nor forgotten.

    Signalling a pgid whose members carry no recognizable marker is the coin flip on
    an unrelated process group that the identity check exists to refuse -- but
    "refused to signal" is not "nothing there", so the record has to survive for a
    later pass rather than being retired as spent.
    """

    if os.name == "nt":
        pytest.skip("process groups outliving their leader is POSIX-specific")

    logger = logging.getLogger(__name__)
    identity = _persisted_identity(pid=4242)
    signalled: list[int] = []

    def _never_reached(pgid, *_args, **_kwargs) -> bool:
        signalled.append(pgid)
        return True

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr("core.process_isolation.probe_process_liveness", lambda _pid: "gone")
        patched.setattr("core.process_isolation.process_group_exists", lambda *_a, **_k: True)
        patched.setattr(
            "core.process_isolation.process_group_identity_status",
            lambda *_a, **_k: "unknown",
        )
        patched.setattr(
            "core.process_isolation.terminate_process_group_by_pgid", _never_reached
        )

        assert reap_orphaned_process_tree(
            logger, "test orphan", expected_identity=identity
        ) == "unconfirmed"
        assert signalled == [], "an unidentifiable group was signalled anyway"

        # A group whose members all carry SOME OTHER tree's marker is ours no longer.
        patched.setattr(
            "core.process_isolation.process_group_identity_status",
            lambda *_a, **_k: "mismatch",
        )
        assert reap_orphaned_process_tree(
            logger, "test orphan", expected_identity=identity
        ) == "gone"
        assert signalled == [], "a recycled pgid was signalled"


def test_process_identity_survives_exec_transition() -> None:
    if os.name == "nt":
        pytest.skip("exec transition is POSIX-specific")

    async def _run() -> None:
        marker = new_process_identity_marker()
        process = await asyncio.create_subprocess_exec(
            os.path.abspath(sys.executable),
            str(Path(watch_worker.__file__).resolve()),
            env=process_identity_subprocess_env(marker),
            stdin=asyncio.subprocess.PIPE,
            **isolated_subprocess_kwargs(),
        )
        try:
            expected = capture_spawned_process_identity(process.pid, marker)
            assert expected is not None
            assert process.stdin is not None
            process.stdin.write(
                watch_worker.encode_watch_worker_spec(
                    command=[
                        "/usr/bin/env",
                        "-i",
                        sys.executable,
                        "-c",
                        "import time; time.sleep(30)",
                    ],
                    shell_command=None,
                )
            )
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.sleep(0.1)
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
    identity_checks = 0

    def open_identity(_pid: int):
        nonlocal identity_checks
        identity_checks += 1
        return SimpleNamespace(pid=12345), identity

    monkeypatch.setattr("core.process_isolation._open_process_identity", open_identity)
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)
    monkeypatch.setattr(os, "killpg", lambda _pgid, sig: signals.append(sig))
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
    assert identity_checks == 3


def test_terminate_process_tree_by_pid_revalidates_before_group_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("process group signalling assertion is POSIX-specific")

    identities = iter(
        [
            _live_identity(),
            _live_identity(worker_fingerprint=fingerprint_process_marker("replacement")),
        ]
    )
    monkeypatch.setattr(
        "core.process_isolation._open_process_identity",
        lambda _pid: (SimpleNamespace(pid=12345), next(identities)),
    )
    monkeypatch.setattr(os, "getpid", lambda: 99999)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 12345)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: pytest.fail("a changed supervisor must not be signaled"),
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
        os,
        "killpg",
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
    monkeypatch.setattr(os, "killpg", lambda _pgid, sig: signals.append(sig))
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
        os,
        "killpg",
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
