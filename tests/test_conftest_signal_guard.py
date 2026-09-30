"""Regression guard: the autouse signal guard must refuse processes a test did
not start, deliver nothing to them, and leave the test's own children signalable.

Without it, a fixture that made ``pid_alive`` true for fake pids let a failed
start roll back through the real ``stop_ui()``, which sent SIGTERM to pid 5678:
on a CI runner that pid was the pytest process itself, and the shard died with
exit code 143.

The same guard bounds the product's process lookups. A desktop start that read
fake pid 1234's environment through psutil was refused on the one runner where a
root process held 1234, and passed everywhere else.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import psutil
import pytest

from core.process_isolation import process_group_exists
from tests.conftest import _REAL_OS_KILL
from tests.fake_pid_helpers import fake_pid
from vibe import runtime

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the guard is POSIX-only; see tests/conftest.py")

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
_NO_SUCH_PID = fake_pid()


def _spawn_detached(
    *, sleeper_session: bool, sleeper: list[str] = _SLEEP, **popen_kwargs
) -> tuple[subprocess.Popen, int]:
    """Start a child that starts a sleeper and exits, orphaning the sleeper."""

    spawner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; "
            f"print(subprocess.Popen({sleeper!r}, start_new_session={sleeper_session}, "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        **popen_kwargs,
    )
    stdout, _ = spawner.communicate(timeout=10)
    return spawner, int(stdout)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def stranger():
    """A live process outside this test's tree: reparented, leading its own session."""

    _, pid = _spawn_detached(sleeper_session=True)
    yield pid
    with suppress(ProcessLookupError):
        _REAL_OS_KILL(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda pid: os.kill(pid, signal.SIGTERM), id="os.kill"),
        pytest.param(lambda pid: os.killpg(pid, signal.SIGTERM), id="os.killpg"),
        pytest.param(lambda pid: psutil.Process(pid).terminate(), id="psutil"),
    ],
)
def test_a_process_the_test_did_not_start_is_refused_and_left_alone(stranger, send, _foreign_signal_guard):
    with pytest.raises(pytest.fail.Exception, match="was not delivered"):
        send(stranger)
    _foreign_signal_guard.violations.clear()

    time.sleep(0.2)
    assert _alive(stranger), "the refused signal reached the process anyway"


def test_the_guard_survives_a_tests_own_monkeypatch_undo(stranger, monkeypatch, _foreign_signal_guard):
    monkeypatch.undo()
    with pytest.raises(pytest.fail.Exception, match="was not delivered"):
        os.kill(stranger, signal.SIGTERM)
    _foreign_signal_guard.violations.clear()

    time.sleep(0.2)
    assert _alive(stranger), "the refused signal reached the process anyway"


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda: os.kill(os.getpid(), signal.SIGURG), id="own-pid"),
        pytest.param(lambda: os.kill(0, signal.SIGURG), id="own-group-via-kill"),
        pytest.param(lambda: os.killpg(os.getpgrp(), signal.SIGURG), id="own-group"),
    ],
)
def test_this_pytest_process_is_refused(send, _foreign_signal_guard):
    # SIGURG is ignored by default, so a guard that let it through would harm
    # nothing else in this process group; the handler makes delivery visible.
    received = []
    previous = signal.signal(signal.SIGURG, lambda *_: received.append(True))
    try:
        with pytest.raises(pytest.fail.Exception, match="was not delivered"):
            send()
        _foreign_signal_guard.violations.clear()
    finally:
        signal.signal(signal.SIGURG, previous)

    assert received == []


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda: os.kill(_NO_SUCH_PID, signal.SIGTERM), id="os.kill"),
        pytest.param(lambda: os.killpg(_NO_SUCH_PID, signal.SIGTERM), id="os.killpg"),
    ],
)
def test_a_target_that_names_no_process_gets_the_real_outcome(send, _foreign_signal_guard):
    # Process-tree teardown signals descendants it collected earlier, and some
    # have exited by then; that is ESRCH, as without the guard, not a failure.
    with pytest.raises(ProcessLookupError):
        send()
    assert _foreign_signal_guard.violations == []


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(lambda child: os.kill(child.pid, signal.SIGTERM), id="os.kill"),
        pytest.param(lambda child: os.killpg(child.pid, signal.SIGTERM), id="os.killpg"),
        pytest.param(lambda child: psutil.Process(child.pid).terminate(), id="psutil"),
    ],
)
def test_a_child_the_test_started_stays_signalable(stop):
    child = subprocess.Popen(_SLEEP, start_new_session=True)
    try:
        stop(child)
        assert child.wait(timeout=10) == -signal.SIGTERM
        # Signalling it once reaped keeps the real outcome instead of a guard failure.
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, signal.SIGTERM)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(lambda leader, orphan: os.killpg(leader, signal.SIGTERM), id="its-group"),
        pytest.param(lambda leader, orphan: os.kill(orphan, signal.SIGTERM), id="itself"),
    ],
)
def test_an_orphan_left_in_a_group_the_test_started_stays_signalable(stop):
    leader, orphan = _spawn_detached(sleeper_session=False, start_new_session=True)
    try:
        assert psutil.Process(orphan).ppid() != os.getpid(), "the sleeper was not reparented"
        stop(leader.pid, orphan)
        assert _wait_until_gone(orphan)
    finally:
        with suppress(ProcessLookupError):
            _REAL_OS_KILL(orphan, signal.SIGKILL)


def test_a_group_member_that_exits_during_the_check_is_not_foreign():
    # Like the Docker entrypoint's shell, the group keeps starting commands that
    # exit at once, so a member listed for the check can be gone when asked about.
    churn = subprocess.Popen(
        ["/bin/sh", "-c", "for _ in 1 2 3 4 5 6 7 8; do (while :; do /bin/true; done) & done; wait"],
        start_new_session=True,
    )
    try:
        time.sleep(0.2)
        for _ in range(1000):
            # Ignored by default, so every round reaches the whole group harmlessly.
            os.killpg(churn.pid, signal.SIGURG)
    finally:
        os.killpg(churn.pid, signal.SIGKILL)
        churn.wait(timeout=10)


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(lambda pid: os.kill(pid, signal.SIGTERM), id="os.kill"),
        pytest.param(lambda pid: os.killpg(pid, signal.SIGTERM), id="os.killpg"),
    ],
)
def test_a_detached_process_naming_this_tests_tmp_path_stays_signalable(stop, tmp_path):
    # Its spawner has exited and it leads its own session, so only the temp
    # directory in its command line ties it to this test.
    _, detached = _spawn_detached(sleeper_session=True, sleeper=[*_SLEEP, str(tmp_path)])
    try:
        stop(detached)
        assert _wait_until_gone(detached)
    finally:
        with suppress(ProcessLookupError):
            _REAL_OS_KILL(detached, signal.SIGKILL)


@pytest.mark.parametrize(
    "named",
    [
        pytest.param(lambda tmp_path: tmp_path.parent / "another_test0", id="another-tests-dir"),
        pytest.param(lambda tmp_path: f"{tmp_path}0", id="a-longer-name-it-prefixes"),
    ],
)
def test_a_detached_process_naming_another_tests_temp_dir_is_refused(named, tmp_path, _foreign_signal_guard):
    # Every test's tmp_path shares the session's temp root, so that root proves nothing.
    _, detached = _spawn_detached(sleeper_session=True, sleeper=[*_SLEEP, str(named(tmp_path))])
    try:
        with pytest.raises(pytest.fail.Exception, match="was not delivered"):
            os.kill(detached, signal.SIGTERM)
        _foreign_signal_guard.violations.clear()

        time.sleep(0.2)
        assert _alive(detached), "the refused signal reached the process anyway"
    finally:
        with suppress(ProcessLookupError):
            _REAL_OS_KILL(detached, signal.SIGKILL)


def test_signal_zero_still_probes_any_pid(stranger):
    os.kill(stranger, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(_NO_SUCH_PID, 0)


@pytest.mark.allow_foreign_signals(reason="asserts the opt-out delivers what the guard would refuse")
def test_the_opt_out_marker_delivers_a_signal_the_guard_would_refuse(stranger):
    os.kill(stranger, signal.SIGTERM)
    assert _wait_until_gone(stranger)


def _free_pid() -> int:
    """A pid nothing holds right now, below every pid limit, so the guard is what answers."""

    for pid in range(99_998, 1, -1):
        try:
            _REAL_OS_KILL(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue
    pytest.skip("every pid below 99999 is taken")


@pytest.mark.parametrize(
    "lookup",
    [
        pytest.param(runtime.process_create_time, id="psutil.Process"),
        pytest.param(runtime.pid_alive, id="signal-0-probe"),
        pytest.param(lambda pid: process_group_exists(pid, logging.getLogger(__name__), "test"), id="group-probe"),
    ],
)
@pytest.mark.parametrize("holder", ["a-stranger", "nothing"])
def test_a_product_lookup_of_a_pid_the_test_did_not_start_fails_whatever_holds_it(
    request, lookup, holder, _foreign_signal_guard
):
    # Failing while nothing holds the pid is what makes a fake pid fail on every
    # machine, instead of only where a real process happens to hold it. The
    # stranger leads its own session, so its pid names a live group too.
    pid = request.getfixturevalue("stranger") if holder == "a-stranger" else _free_pid()
    with pytest.raises(pytest.fail.Exception, match="neither started nor found by listing"):
        lookup(pid)
    _foreign_signal_guard.violations.clear()


def test_a_fake_pid_names_no_process_on_any_machine(_foreign_signal_guard):
    pid = fake_pid()

    assert runtime.process_create_time(pid) is None
    assert runtime.pid_alive(pid) is False
    assert process_group_exists(pid, logging.getLogger(__name__), "test") is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    assert _foreign_signal_guard.violations == []
