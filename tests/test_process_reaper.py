"""No process a ``reap_processes`` test starts outlives it, however it ends (tests/process_reaper.py).

The run-level cases run a pytest of their own with this suite's conftest, so the
reaper and the warden act exactly as they do for every marked test file.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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
# connector and exits, so the connector reparents to init. Unless the mode is
# `skip` it records itself and the spawn first, as the remote-access helpers do;
# `late` spawns a second after the test has returned, and `group` spawns a shell
# that leaves a background member in its group when it exits.
DETACHING = '''
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests import process_reaper

pytestmark = [pytest.mark.reap_processes, pytest.mark.usefixtures("reap_helper_spawns")]

HELPER = (
    "import os, subprocess, sys, time\\n"
    "from pathlib import Path\\n"
    "import psutil\\n"
    "mode, connector, record = sys.argv[1:4]\\n"
    "if mode != 'skip':\\n"
    "    from tests import process_reaper\\n"
    "    process_reaper.record_spawns(Path(record))\\n"
    "if mode == 'late':\\n"
    "    time.sleep(1)\\n"
    "args = ['/bin/sh', '-c', 'sleep 600 & sleep 0.5'] if mode == 'group' else [connector]\\n"
    "spawn = subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\\n"
    "created = psutil.Process(spawn.pid).create_time()\\n"
    "Path(os.environ['REAPER_PROBE_RECORD']).write_text(f'{spawn.pid} {created!r}\\\\n')\\n"
)


def _run_helper(tmp_path, mode):
    connector = tmp_path / "bin" / "cloudflared"
    connector.parent.mkdir()
    connector.write_text("#!/bin/sh\\nwhile :; do sleep 0.2; done\\n")
    connector.chmod(0o755)
    record = process_reaper.helper_spawn_record(tmp_path)
    helper = subprocess.Popen([sys.executable, "-c", HELPER, mode, str(connector), str(record)])
    if mode != "late":
        assert helper.wait() == 0


def test_a_recorded_spawn(tmp_path):
    _run_helper(tmp_path, "record")


def test_an_unrecorded_orphan(tmp_path):
    _run_helper(tmp_path, "skip")


def test_a_spawn_recorded_after_the_test_returned(tmp_path):
    _run_helper(tmp_path, "late")


def test_a_group_member_left_by_a_recorded_leader(tmp_path):
    _run_helper(tmp_path, "group")


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


def _group_of(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


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


def test_a_spawn_a_helper_records_during_teardown_is_reaped(tmp_path):
    result, record = _run_inner(tmp_path, "recorded_after_the_test_returned", DETACHING)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    pid = record.read_text().split()[0]
    assert f"were still running and were killed: {pid} (" in output, output
    assert _running(record) == []


def test_a_group_member_a_recorded_leader_left_is_killed_and_fails_its_test(tmp_path):
    result, record = _run_inner(tmp_path, "group_member_left", DETACHING)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "(sleep)" in _reported(output, "were still running and were killed: "), output
    leader = int(record.read_text().split()[0])
    assert [process.pid for process in psutil.process_iter() if _group_of(process.pid) == leader] == []


@pytest.mark.allow_foreign_signals(reason="ends the orphan the inner run deliberately left unrecorded")
def test_an_unrecorded_orphan_is_reported_and_never_signalled(tmp_path):
    try:
        result, record = _run_inner(tmp_path, "unrecorded_orphan", DETACHING)
        pid = int(record.read_text().split()[0])
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        reported = _reported(output, "are in no spawn record and were left running: ")
        assert reported.startswith(f"{pid} (") and "/" not in reported
        assert _running(record) == [pid]
    finally:
        # No reaper knows this orphan, so the test ends it by the identity the
        # helper wrote, however the inner run ended. It leads its own group:
        # its shell's sleeps end with it.
        written = tmp_path / "spawned"
        for running in _running(written) if written.exists() else []:
            os.killpg(running, signal.SIGKILL)


def test_a_process_with_a_live_parent_naming_tmp_path_is_neither_killed_nor_reported(tmp_path):
    # A developer's `tail -f` on a test log: it names tmp_path, but its parent lives.
    watcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", str(tmp_path / "connector.log")])
    try:
        assert process_reaper.end_helper_spawns(process_reaper.helper_spawn_record(tmp_path), tmp_path) == ([], [])
        assert watcher.poll() is None
    finally:
        watcher.kill()
        watcher.wait()


# A helper whose record write fails after `Popen` has started the child: the
# disk filling up between the spawn and its fsync. The child, a shell leading a
# session of its own, has put a command in the background by then and, with
# `exit`, already exited.
FAILING_RECORD = """
import os, signal, subprocess, sys, time
from pathlib import Path

import psutil

from tests import process_reaper

process_reaper.record_spawns(Path(sys.argv[1]))
spawned = []


def in_group(pgid):
    found = []
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid and process.status() != psutil.STATUS_ZOMBIE:
                found.append(process)
        except (OSError, psutil.Error):
            pass
    return found


def fail(fd):
    shell = psutil.Process().children()[0].pid
    spawned.append(shell)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            exited = psutil.Process(shell).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            exited = True
        if exited and in_group(shell):
            break
        time.sleep(0.01)
    raise OSError(28, "No space left on device")


os.fsync = fail
try:
    subprocess.Popen(["/bin/sh", "-c", "sleep 600 & exit 0"], start_new_session=True)
except OSError as exc:
    print("raised", exc.errno)
left = [process for pgid in spawned for process in in_group(pgid)]
print("left", len(left))
for pgid in spawned:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""


def test_a_spawn_that_cannot_be_recorded_is_ended_before_the_failure_propagates(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", FAILING_RECORD, str(process_reaper.helper_spawn_record(tmp_path))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=hang_guard.HANG_GUARD_SECONDS,
    )

    assert result.stdout.splitlines() == ["raised 28", "left 0"], result.stdout + result.stderr


# A recorded helper that waits on a control pipe and, when told, spawns a
# detached child its record never names (`posix_spawn` bypasses the `Popen`
# wrapper), as a helper killed before its append would leave one.
CONTROLLED_HELPER = """
import os, sys
from pathlib import Path

import psutil

from tests import process_reaper

process_reaper.record_spawns(Path(sys.argv[1]))
written = Path(sys.argv[2])
for line in sys.stdin:
    pid = os.posix_spawn(sys.executable, [sys.executable, "-c", "import time; time.sleep(600)"], os.environ, setsid=True)
    written.write_text(f"{pid} {psutil.Process(pid).create_time()!r}\\n")
"""


@pytest.mark.allow_foreign_signals(reason="ends the child an unfrozen reap would leave, by the pid the helper wrote")
def test_a_helper_is_frozen_before_the_last_snapshot_so_it_spawns_nothing_before_its_kill(tmp_path, monkeypatch):
    record = process_reaper.helper_spawn_record(tmp_path)
    written = tmp_path / "late-child"
    helper = subprocess.Popen(
        [sys.executable, "-c", CONTROLLED_HELPER, str(record), str(written)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        text=True,
    )
    kill_and_wait = process_reaper._kill_and_wait

    def ask_the_helper_to_spawn_then_kill(targets, groups):
        # Between the last snapshot and the kill: a running helper spawns at once.
        helper.stdin.write("spawn\n")
        helper.stdin.flush()
        give_up = time.monotonic() + 1.0
        while not written.exists() and time.monotonic() < give_up:
            time.sleep(0.01)
        kill_and_wait(targets, groups)

    try:
        give_up = time.monotonic() + hang_guard.HANG_GUARD_SECONDS
        while not record.exists() and time.monotonic() < give_up:
            time.sleep(0.01)
        monkeypatch.setattr(process_reaper, "_SPAWN_EXIT_SECONDS", 0.2)
        monkeypatch.setattr(process_reaper, "_kill_and_wait", ask_the_helper_to_spawn_then_kill)

        reaped, orphans = process_reaper.end_helper_spawns(record, tmp_path)

        # Stopped, the helper never read the request: no child exists to outlive it.
        assert not written.exists()
        assert len(reaped) == 1 and reaped[0].startswith(f"{helper.pid} (") and orphans == []
    finally:
        if written.exists():
            for pid in _running(written):
                os.killpg(pid, signal.SIGKILL)
        if helper.poll() is None:
            helper.kill()
        helper.wait()


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
