"""Late shutdown evidence must survive logging drain without holding exit open.

Ordinary logging tests cannot observe Python's final worker join, nor prove a
new diagnostic sink cannot block the cleanup thread. Each subprocess below
owns only temporary paths and its own threads; no running service is touched.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from vibe import shutdown_diagnostics


def _child(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1]), "HOME": str(tmp_path)}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=12,
        check=True,
    )


def test_snapshot_survives_logging_shutdown_and_main_return(tmp_path):
    result = _child(tmp_path, """
        import concurrent.futures
        import json
        import logging
        import sys
        import threading
        import time
        from pathlib import Path
        from vibe.shutdown_diagnostics import ShutdownDiagnostics

        root = Path(sys.argv[1])
        entered = threading.Event()
        release = threading.Event()
        def held_worker():
            secret_local = 'LOCAL_VALUE_MUST_NOT_APPEAR'
            entered.set()
            release.wait(10)
            return secret_local

        # A real executor join after main returns is the previously silent tail.
        executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix='held-cleanup')
        executor.submit(held_worker)
        assert entered.wait(2)
        diagnostics = ShutdownDiagnostics(root)
        diagnostics.start()
        path = diagnostics.path
        diagnostics.start()  # Duplicate signals cannot rearm or open a second file.
        assert diagnostics.path == path
        logging.shutdown()
        diagnostics.phase('logging_drain_return')
        diagnostics.phase('main_return')

        def observe():
            until = time.monotonic() + 8
            try:
                while time.monotonic() < until:
                    text = path.read_text() if path.exists() else ''
                    if 'in held_worker' in text and 'Shutdown phase=thread_executor_join_start' in text:
                        print(json.dumps({'text': text, 'file_count': len(list(root.glob('shutdown-*.log')))}))
                        return
                    time.sleep(0.01)
                raise AssertionError('no late stack snapshot')
            finally:
                release.set()
        threading.Thread(target=observe, daemon=True).start()
        # No executor shutdown here: interpreter teardown must do the join.
    """)
    observed = json.loads(result.stdout)
    text = observed["text"]
    assert observed["file_count"] == 1
    assert text.count("Shutdown phase=thread_snapshot") == 1
    assert "held-cleanup_0" in text
    assert "MainThread" in text
    assert "in _python_exit" in text
    assert "in held_worker" in text
    assert "LOCAL_VALUE_MUST_NOT_APPEAR" not in text
    assert "secret_local" not in text  # Neither locals nor source lines.
    phases = [line for line in text.splitlines() if "Shutdown phase=" in line]
    phase_names = [line.split("Shutdown phase=")[1] for line in phases]
    assert "main_return" in phase_names
    assert "thread_executor_join_start" in phase_names
    phase_elapsed = {
        name: int(line.split("elapsed_ms=")[1].split()[0])
        for line, name in zip(phases, phase_names)
    }
    assert phase_elapsed["main_return"] < phase_elapsed["thread_executor_join_start"]
    snapshot_ms = int(phases[-1].split("elapsed_ms=")[1].split()[0])
    assert 4000 <= snapshot_ms < 8000
    if os.name != "nt":
        assert stat.S_IMODE(next(tmp_path.glob("shutdown-*.log")).stat().st_mode) == 0o600


def test_sync_phase_is_visible_before_the_caller_returns(tmp_path):
    diagnostics = shutdown_diagnostics.ShutdownDiagnostics(tmp_path)
    diagnostics.start()
    assert diagnostics.path is not None

    deadline = time.monotonic() + 2
    while not diagnostics.path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert diagnostics.path.exists()

    diagnostics.phase_sync("atexit_test_hook_start")

    text = diagnostics.path.read_text()
    assert "Shutdown phase=atexit_test_hook_start" in text


def test_shutdown_hook_wraps_before_and_after_markers(monkeypatch):
    phases = []
    monkeypatch.setattr(
        shutdown_diagnostics,
        "_ACTIVE_DIAGNOSTICS",
        type("Diagnostics", (), {"phase_sync": lambda self, phase: phases.append(phase)})(),
    )
    registered = []
    monkeypatch.setattr(shutdown_diagnostics.atexit, "register", registered.append)

    def hook():
        phases.append("hook_body")

    shutdown_diagnostics.register_shutdown_hook("test_hook", hook)
    wrapped = registered[0]
    wrapped()

    assert phases == ["atexit_test_hook_start", "hook_body", "atexit_test_hook_return"]


def test_partial_executor_exit_callback_is_instrumented(monkeypatch):
    import concurrent.futures.thread as executor_thread
    import functools

    callbacks = [functools.partial(executor_thread._python_exit)]
    monkeypatch.setattr(shutdown_diagnostics.threading, "_threading_atexits", callbacks)
    monkeypatch.setattr(shutdown_diagnostics, "_EXECUTOR_EXIT_HOOKS_INSTALLED", False)

    shutdown_diagnostics.install_executor_exit_diagnostics()

    assert getattr(callbacks[0], "_avibe_shutdown_diagnostic", False)


@pytest.mark.parametrize("name", ["thread_executor_join", "process_executor_join"])
def test_executor_exit_marker_wrapper(monkeypatch, name):
    phases = []
    monkeypatch.setattr(
        shutdown_diagnostics,
        "_ACTIVE_DIAGNOSTICS",
        type("Diagnostics", (), {"phase_sync": lambda self, phase: phases.append(phase)})(),
    )
    callback = shutdown_diagnostics._wrap_threading_exit_hook(
        lambda: phases.append("join_body"), name,
    )

    callback()

    assert phases == [f"{name}_start", "join_body", f"{name}_return"]


@pytest.mark.parametrize("sink", ["healthy", "blocked", "failed"])
def test_diagnostics_never_holds_process_exit(tmp_path, sink):
    # Deadline is made huge, not shorter: waiting on this daemon would make the
    # subprocess time out. A held open also proves producers perform no sink I/O.
    script = """
        import sys
        import threading
        from pathlib import Path
        from vibe import shutdown_diagnostics as module
        module.STACK_DELAY_SECONDS = 3600
        sink = SINK
        if sink == 'blocked':
            def blocked_open(*args, **kwargs):
                threading.Event().wait(3600)
            module.os.open = blocked_open
            module._PRIVATE_OPEN = blocked_open
        elif sink == 'failed':
            def failed_open(*args, **kwargs):
                raise OSError('test-owned sink failure')
            module.os.open = failed_open
            module._PRIVATE_OPEN = failed_open
        diagnostics = module.ShutdownDiagnostics(Path(sys.argv[1]))
        assert diagnostics.path is None
        diagnostics.phase('not_started')
        diagnostics.start()
        diagnostics.phase('controller_run_return')
        diagnostics.phase('main_return')
        print('cleanup returned')
    """.replace("SINK", repr(sink))
    assert _child(tmp_path, script).stdout.strip() == "cleanup returned"


def test_failed_thread_start_does_not_prevent_cleanup(monkeypatch, tmp_path):
    def fail_start(self):
        raise RuntimeError("test-owned thread start failure")

    monkeypatch.setattr(shutdown_diagnostics.threading.Thread, "start", fail_start)
    diagnostics = shutdown_diagnostics.ShutdownDiagnostics(tmp_path)
    diagnostics.start()
    diagnostics.phase("main_return")
    diagnostics.start()
    assert not list(tmp_path.iterdir())
