"""No process a test starts outlives it, however the test ends (tests/process_reaper.py).

Each case runs a pytest of its own with this suite's conftest, so the reaper and
the warden act exactly as they do for every test file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests import hang_guard

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the reaper records spawns on POSIX only")

REPO_ROOT = Path(__file__).resolve().parents[1]

# What leaked before: a child stopped with SIGSTOP in a session of its own (the
# job wrapper the deadline test stops), and a grandchild a spawned shell put in
# the background, orphaned once the shell exits and found only through its group.
INNER = '''
import os
import signal
import subprocess
import sys
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
    RECORD.write_text(
        "".join(f"{pid} {psutil.Process(pid).create_time()!r}\\n" for pid in (stopped.pid, orphan))
    )


def test_a_failing_body():
    _spawn_tree()
    raise AssertionError("fails on purpose")


def test_then_nothing_it_started_is_running():
    for line in RECORD.read_text().splitlines():
        pid, created = line.split()
        try:
            process = psutil.Process(int(pid))
            assert process.create_time() != float(created) or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass


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
        try:
            process = psutil.Process(int(pid))
            if process.create_time() == float(created) and process.status() != psutil.STATUS_ZOMBIE:
                running.append(process.pid)
        except psutil.NoSuchProcess:
            pass
    return running


def test_a_failed_test_leaves_nothing_it_started_running(tmp_path):
    result, record = _run_inner(tmp_path, "failing_body or nothing_it_started")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "1 failed, 1 passed" in output, output
    assert "test_a_failing_body" in output.split("processes still running when their test ended were killed")[1]
    assert _running(record) == []


async def test_a_killed_run_leaves_nothing_it_started_running(tmp_path):
    result, record = _run_inner(tmp_path, "killed_run")

    assert result.returncode == -9, result.stdout + result.stderr
    # The warden acts once it sees the run gone, after this pytest saw it exit.
    await hang_guard.until(lambda: _running(record) == [], "the warden to kill what the killed run started")
