from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from core import watch_worker
from core.process_isolation import PROCESS_IDENTITY_ENV


def _supervisor_command() -> list[str]:
    return [
        os.path.abspath(sys.executable),
        str(Path(watch_worker.__file__).resolve()),
    ]


def _supervisor_isolation_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def test_watch_worker_hides_supervisor_identity_and_preserves_empty_arguments() -> None:
    marker = "supervisor-only-marker"
    env = dict(os.environ)
    env[PROCESS_IDENTITY_ENV] = marker
    spec = watch_worker.encode_watch_worker_spec(
        command=[
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "print(os.environ.get('AVIBE_PROCESS_IDENTITY', 'missing')); "
                "print(repr(sys.argv[1])); "
                "print('worker-stderr', file=sys.stderr)"
            ),
            "",
        ],
        shell_command=None,
    )

    result = subprocess.run(
        _supervisor_command(),
        input=spec,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
        **_supervisor_isolation_kwargs(),
    )

    assert result.returncode == 0
    assert result.stdout.decode().splitlines() == ["missing", "''"]
    assert result.stderr.decode().strip() == "worker-stderr"
    assert marker not in result.stdout.decode()
    assert marker not in result.stderr.decode()


def test_watch_worker_rejects_invalid_specification() -> None:
    result = subprocess.run(
        _supervisor_command(),
        input=b'{"version":1,"command":[],"shell_command":null}',
        capture_output=True,
        timeout=10,
        check=False,
        **_supervisor_isolation_kwargs(),
    )

    assert result.returncode == 1
    assert "invalid watch worker command" in result.stderr.decode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group contract")
def test_posix_watch_worker_survives_term_until_waiter_exits(tmp_path: Path) -> None:
    waiter_pid_path = tmp_path / "waiter.pid"
    process = subprocess.Popen(
        _supervisor_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_supervisor_isolation_kwargs(),
    )
    assert process.stdin is not None
    process.stdin.write(
        watch_worker.encode_watch_worker_spec(
            command=[
                sys.executable,
                "-c",
                (
                    "import os,signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"Path({str(waiter_pid_path)!r}).write_text(str(os.getpid())); "
                    "time.sleep(30)"
                ),
            ],
            shell_command=None,
        )
    )
    process.stdin.close()

    try:
        deadline = time.monotonic() + 5
        while not waiter_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        waiter_pid = int(waiter_pid_path.read_text())
        os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.2)
        assert process.poll() is None
        assert psutil.pid_exists(waiter_pid)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group contract")
def test_posix_watch_worker_waits_for_descendant_after_command_root_exits() -> None:
    child_script = (
        "import subprocess,sys; "
        "child=subprocess.Popen("
        "[sys.executable,'-c','import time; time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
        "); "
        "print(child.pid, flush=True)"
    )
    process = subprocess.Popen(
        _supervisor_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_supervisor_isolation_kwargs(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        watch_worker.encode_watch_worker_spec(
            command=[sys.executable, "-c", child_script],
            shell_command=None,
        )
    )
    process.stdin.close()
    descendant_pid = int(process.stdout.readline().decode().strip())

    try:
        time.sleep(0.2)
        assert process.poll() is None
        assert psutil.pid_exists(descendant_pid)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        if psutil.pid_exists(descendant_pid):
            psutil.Process(descendant_pid).kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_watch_worker_job_tracks_descendant_after_command_root_exits() -> None:
    child_script = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(child.pid, flush=True)"
    )
    process = subprocess.Popen(
        _supervisor_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **_supervisor_isolation_kwargs(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        watch_worker.encode_watch_worker_spec(
            command=[sys.executable, "-c", child_script],
            shell_command=None,
        )
    )
    process.stdin.close()
    descendant_pid = int(process.stdout.readline().decode().strip())

    try:
        assert process.poll() is None
        process.terminate()
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while psutil.pid_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(descendant_pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if psutil.pid_exists(descendant_pid):
            psutil.Process(descendant_pid).kill()


@pytest.mark.skipif(os.name != "nt", reason="Windows exit code contract")
def test_windows_watch_worker_preserves_full_exit_code() -> None:
    result = subprocess.run(
        _supervisor_command(),
        input=watch_worker.encode_watch_worker_spec(
            command=[sys.executable, "-c", "import os; os._exit(513)"],
            shell_command=None,
        ),
        capture_output=True,
        timeout=10,
        check=False,
        **_supervisor_isolation_kwargs(),
    )

    assert result.returncode == 513
