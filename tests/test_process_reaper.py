"""No process a test starts outlives it, however the test ends (tests/process_reaper.py).

The run-level cases run a pytest of their own with this suite's conftest, so the
reaper and the warden act exactly as they do for every test file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests import hang_guard, process_reaper

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the reaper records spawns on POSIX only")

REPO_ROOT = Path(__file__).resolve().parents[1]
SUMMARY = "processes still running when their test ended were killed"

# What leaked before, and every other way a test starts a child: a child stopped
# with SIGSTOP in a session of its own (the job wrapper the deadline test stops),
# a grandchild a spawned shell put in the background, orphaned once the shell
# exits and found only through its group, a multiprocessing worker, and a pty
# child. The spawn context also starts multiprocessing's resource tracker, which
# serves the whole run and must survive the reap.
INNER = '''
import multiprocessing
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

RECORD = Path(os.environ["REAPER_PROBE_RECORD"])


def _spawn_tree():
    stopped = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    os.kill(stopped.pid, signal.SIGSTOP)
    shell = subprocess.Popen(
        ["sh", "-c", "sleep 600 & echo $!"], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    orphan = int(shell.stdout.readline())
    shell.wait()
    worker = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(600,))
    worker.start()
    pid, _ = pty.fork()
    if pid == 0:
        os.execv("/bin/sleep", ["sleep", "600"])
    RECORD.write_text(
        "".join(
            f"{each} {psutil.Process(each).create_time()!r}\\n" for each in (stopped.pid, orphan, worker.pid, pid)
        )
    )


def _tracker():
    from multiprocessing import resource_tracker

    return resource_tracker._resource_tracker._pid


def test_a_failing_body():
    _spawn_tree()
    assert _tracker() is not None
    raise AssertionError("fails on purpose")


def test_then_nothing_it_started_is_running():
    for line in RECORD.read_text().splitlines():
        pid, created = line.split()
        try:
            process = psutil.Process(int(pid))
            assert process.create_time() != float(created) or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    assert psutil.Process(_tracker()).status() != psutil.STATUS_ZOMBIE


@pytest.mark.allow_foreign_signals(reason="the run kills itself, as the CI watchdog does")
def test_a_killed_run():
    _spawn_tree()
    os.kill(os.getpid(), signal.SIGKILL)
'''


def _run_inner(tmp_path: Path, selection: str) -> tuple[subprocess.CompletedProcess, Path]:
    inner = tmp_path / "test_inner.py"
    inner.write_text(INNER)
    record = tmp_path / "spawned"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-q", str(inner), "-k", selection],
        cwd=REPO_ROOT,
        env={**os.environ, "REAPER_PROBE_RECORD": str(record)},
        capture_output=True,
        text=True,
        timeout=hang_guard.HANG_GUARD_SECONDS,
    )
    return result, record


def _running(record: Path) -> list[int]:
    running = []
    for line in record.read_text().splitlines():
        pid, created = line.split()
        if process_reaper._same(int(pid), float(created)) is not None:
            running.append(int(pid))
    return running


def test_a_failed_test_leaves_nothing_it_started_running(tmp_path):
    result, record = _run_inner(tmp_path, "failing_body or nothing_it_started")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed, 1 passed" in output, output
    reported = output.split(SUMMARY)[1]
    assert "test_a_failing_body" in reported
    # A report names the process, never its arguments, which can carry a credential.
    assert "time.sleep" not in reported and "sleep 600" not in reported
    assert _running(record) == []


async def test_a_killed_run_leaves_nothing_it_started_running(tmp_path):
    result, record = _run_inner(tmp_path, "killed_run")

    assert result.returncode == -9, result.stdout + result.stderr
    # The warden acts once it sees the run gone, after this pytest saw it exit.
    await hang_guard.until(lambda: _running(record) == [], "the warden to kill what the killed run started")


@pytest.fixture
def group_leader():
    """A live process leading its own group, as a stranger that reused a recorded pid would."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
    try:
        yield process.pid, psutil.Process(process.pid).create_time()
    finally:
        process.kill()
        process.wait()


def test_a_reap_never_signals_a_group_whose_pid_now_names_another_process(group_leader):
    pid, created = group_leader
    processes = process_reaper.TestProcesses(None)
    processes._spawned[pid] = process_reaper._Spawn(created - 1.0, founded_group=True)

    assert processes.reap() == []

    assert process_reaper._same(pid, created) is not None


@pytest.mark.parametrize("settled", [False, True], ids=["stale", "settled"])
def test_the_warden_never_signals_a_record_it_cannot_prove_is_still_the_runs(group_leader, settled):
    pid, created = group_leader
    warden = process_reaper.Warden()
    if settled:
        # Forgotten once a reap confirmed the test's records gone, even an exact match.
        warden.record(pid, created, founded_group=True)
        warden.settle()
    else:
        warden.record(pid, created - 1.0, founded_group=True)

    warden.close()

    assert process_reaper._same(pid, created) is not None
