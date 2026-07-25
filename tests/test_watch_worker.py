from __future__ import annotations

import os
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
        str(Path(sys.executable).resolve()),
        str(Path(watch_worker.__file__).resolve()),
    ]


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
    )

    assert result.returncode == 1
    assert "invalid watch worker command" in result.stderr.decode()


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
