"""No process a ``reap_processes`` test starts outlives it, however it ends (tests/process_reaper.py).

The run-level cases run a pytest of their own with this suite's conftest, so the
reaper and the warden act exactly as they do for every marked test file.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil
import pytest

from tests import hang_guard, process_reaper

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the reaper records spawns on POSIX only")

REPO_ROOT = Path(__file__).resolve().parents[1]
SUMMARY = "processes still running when their test ended were killed"

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
pytestmark = pytest.mark.reap_processes


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


# A run without a marked test is untouched: no warden, nothing recorded.
UNMARKED = '''
import psutil


def test_an_unmarked_run_starts_no_warden():
    assert psutil.Process().children() == []
'''


# A helper interpreter, as a cross-process child is: it spawns a detached
# connector and exits, so the connector reparents to init. With `record` it
# records the spawn first, as the remote-access helpers do.
DETACHING = '''
import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests import process_reaper

RECORD = Path(os.environ["REAPER_PROBE_RECORD"])
pytestmark = pytest.mark.usefixtures("reap_helper_spawns")

HELPER = (
    "import subprocess, sys\\n"
    "from pathlib import Path\\n"
    "if sys.argv[2] == 'record':\\n"
    "    from tests import process_reaper\\n"
    "    process_reaper.record_spawns(Path(sys.argv[3]))\\n"
    "print(subprocess.Popen([sys.argv[1]], start_new_session=True, stdin=subprocess.DEVNULL,"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)\\n"
)


def _detach_a_connector(tmp_path, mode):
    connector = tmp_path / "bin" / "cloudflared"
    connector.parent.mkdir()
    connector.write_text("#!/bin/sh\\nwhile :; do sleep 0.2; done\\n")
    connector.chmod(0o755)
    record = process_reaper.helper_spawn_record(tmp_path)
    helper = subprocess.run(
        [sys.executable, "-c", HELPER, str(connector), mode, str(record)], capture_output=True, text=True, check=True
    )
    pid = int(helper.stdout)
    RECORD.write_text(f"{pid} {psutil.Process(pid).create_time()!r}\\n")


def test_a_recorded_spawn(tmp_path):
    _detach_a_connector(tmp_path, "record")


def test_an_unrecorded_orphan(tmp_path):
    _detach_a_connector(tmp_path, "skip")


@pytest.mark.allow_foreign_signals(reason="the run kills itself, as the CI watchdog does")
def test_a_killed_run_after_a_recorded_spawn(tmp_path):
    _detach_a_connector(tmp_path, "record")
    os.kill(os.getpid(), signal.SIGKILL)
'''


def _run_inner(tmp_path: Path, selection: str, source: str = INNER) -> tuple[subprocess.CompletedProcess, Path]:
    inner = tmp_path / "test_inner.py"
    inner.write_text(source)
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


def test_a_run_without_a_marked_test_has_no_warden(tmp_path):
    result, _ = _run_inner(tmp_path, "unmarked", UNMARKED)

    assert result.returncode == 0, result.stdout + result.stderr


def _reported(output: str, after: str) -> str:
    return output.split(after)[1].splitlines()[0]


def test_a_recorded_spawn_is_killed_by_identity_and_fails_its_test(tmp_path):
    result, record = _run_inner(tmp_path, "test_a_recorded_spawn", DETACHING)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    # The body passed; the teardown reaped the spawn and failed the test.
    assert "1 passed" in output and "1 error" in output, output
    pid = record.read_text().split()[0]
    reported = _reported(output, "were still running and were killed: ")
    # A report names the process, never its arguments, which can carry a credential.
    assert reported.startswith(f"{pid} (") and "/" not in reported
    assert _running(record) == []


async def test_a_recorded_spawn_ends_with_a_killed_run(tmp_path):
    result, record = _run_inner(tmp_path, "killed_run_after_a_recorded_spawn", DETACHING)

    assert result.returncode == -9, result.stdout + result.stderr
    # The warden acts once it sees the run gone, after this pytest saw it exit.
    await hang_guard.until(lambda: _running(record) == [], "the warden to reap the recorded spawn")


@pytest.mark.allow_foreign_signals(reason="ends the orphan the inner run deliberately left unrecorded")
def test_an_unrecorded_orphan_is_reported_and_never_signalled(tmp_path):
    result, record = _run_inner(tmp_path, "unrecorded_orphan", DETACHING)
    pid = int(record.read_text().split()[0])
    try:
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        reported = _reported(output, "are in no spawn record and were left running: ")
        assert reported.startswith(f"{pid} (") and "/" not in reported
        assert _running(record) == [pid]
    finally:
        # The orphan leads its own group: its shell's sleeps end with it.
        os.killpg(pid, signal.SIGKILL)


def test_a_process_with_a_live_parent_naming_tmp_path_is_neither_killed_nor_reported(tmp_path):
    # A developer's `tail -f` on a test log: it names tmp_path, but its parent lives.
    watcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", str(tmp_path / "connector.log")])
    try:
        assert process_reaper.end_helper_spawns(process_reaper.helper_spawn_record(tmp_path), tmp_path) == ([], [])
        assert watcher.poll() is None
    finally:
        watcher.kill()
        watcher.wait()


@pytest.mark.parametrize("where", ["outside", "basetemp"])
def test_a_spawn_record_not_inside_the_basetemp_is_refused(tmp_path_factory, where):
    basetemp = tmp_path_factory.getbasetemp()
    record = Path(tempfile.gettempdir()) / "helper-spawns" if where == "outside" else basetemp
    warden = process_reaper.Warden()
    try:
        with pytest.raises(pytest.fail.Exception, match="not strictly inside"):
            warden.watch_spawns(record, basetemp)
    finally:
        warden.close()


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
