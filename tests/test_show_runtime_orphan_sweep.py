"""Regression tests for Show Runtime orphan reaping (avibe#813).

Covers the two avibe-side layers that stop a Node ``cli.js`` runtime server from
outliving the service that spawned it:

* the pure matcher that recognises a runtime server bound to a workspace root,
* the sweep that terminates such orphans (exercised against real processes), and
* the wiring that reaps the runtime on the controller's synchronous shutdown path.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from unittest.mock import patch

import psutil

from core import show_runtime
from core.show_runtime import _is_runtime_server_cmdline, sweep_orphan_show_runtime_servers


def _runtime_cmdline(workspace_root: str) -> list[str]:
    """Mirror the argv shape spawned by ShowRuntimeManager.ensure()."""
    return [
        "node",
        "/opt/show-runtime/dist/cli.js",
        "--workspace-root",
        workspace_root,
        "--cache-root",
        "/tmp/cache",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--fallback-delay-seconds",
        "8",
    ]


# --------------------------------------------------------------------------- matcher


def test_matcher_matches_runtime_bound_to_root():
    root = "/tmp/avibe-show/ws"
    assert _is_runtime_server_cmdline(_runtime_cmdline(root), root) is True


def test_matcher_rejects_different_workspace_root():
    assert _is_runtime_server_cmdline(_runtime_cmdline("/tmp/a"), "/tmp/b") is False


def test_matcher_rejects_unrelated_process_mentioning_path():
    root = "/tmp/avibe-show/ws"
    # A tool that merely references the path but is not the runtime server.
    assert _is_runtime_server_cmdline(["grep", "-r", "needle", root], root) is False


def test_matcher_requires_signature_not_just_the_flag():
    root = "/tmp/avibe-show/ws"
    # Exact --workspace-root pair but no runtime signature -> not a match.
    assert _is_runtime_server_cmdline(["some-tool", "--workspace-root", root], root) is False


def test_matcher_matches_managed_bin_without_cli_js():
    root = "/tmp/avibe-show/ws"
    cmd = [f"/home/avibe/.local/bin/{show_runtime._RUNTIME_BIN}", "--workspace-root", root, "--fallback-delay-seconds", "8"]
    assert _is_runtime_server_cmdline(cmd, root) is True


def test_matcher_handles_empty_cmdline():
    assert _is_runtime_server_cmdline([], "/tmp/x") is False


# ----------------------------------------------------------------------------- sweep


def _spawn_fake_orphan(workspace_root: str) -> subprocess.Popen:
    """Spawn a real, long-lived process whose argv matches a runtime server.

    psutil reads argv, so a plain Python sleeper with a runtime-shaped command
    line is indistinguishable from a real orphan for the purposes of the sweep.
    ``start_new_session`` mirrors the real spawn (its own session/pgroup).
    """
    argv = [
        sys.executable,
        "-c",
        "import time; time.sleep(120)",
        "cli.js",
        "--workspace-root",
        workspace_root,
        "--fallback-delay-seconds",
        "8",
    ]
    return subprocess.Popen(argv, start_new_session=True)


def _gone(pid: int, timeout: float = 5.0) -> bool:
    """True once ``pid`` no longer exists (a reaped zombie counts as gone)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _reap(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass


def test_sweep_kills_orphan_bound_to_root(tmp_path):
    root = str(tmp_path / "show")
    proc = _spawn_fake_orphan(root)
    try:
        swept = sweep_orphan_show_runtime_servers(root)
        assert proc.pid in swept
        assert _gone(proc.pid), "orphan bound to the workspace root should be terminated"
    finally:
        _reap(proc)


def test_sweep_spares_process_on_a_different_root(tmp_path):
    ours = str(tmp_path / "ours")
    other = str(tmp_path / "other")
    proc = _spawn_fake_orphan(other)
    try:
        swept = sweep_orphan_show_runtime_servers(ours)
        assert proc.pid not in swept
        assert proc.poll() is None, "a server on a different workspace root must be spared"
    finally:
        _reap(proc)


def test_sweep_respects_keep_pid(tmp_path):
    root = str(tmp_path / "show")
    proc = _spawn_fake_orphan(root)
    try:
        swept = sweep_orphan_show_runtime_servers(root, keep_pid=proc.pid)
        assert proc.pid not in swept
        assert proc.poll() is None, "keep_pid (our own live child) must be spared"
    finally:
        _reap(proc)


# ------------------------------------------------ UI server process lifecycle wiring
#
# The Node runtime is a child of the UI server process (run_ui_server), so the
# startup sweep and shutdown reap must live there — not in the controller process,
# which never spawns a runtime.


def test_ui_server_registers_startup_sweep_and_shutdown_reap():
    from vibe import ui_server

    assert ui_server.sweep_orphan_show_runtime_servers_on_startup in ui_server.app.router.on_startup
    assert ui_server.stop_show_runtime_on_shutdown in ui_server.app.router.on_shutdown


def test_startup_handler_sweeps_orphans():
    from vibe import ui_server

    with patch("core.show_runtime.sweep_orphan_show_runtime_servers") as sweep:
        asyncio.run(ui_server.sweep_orphan_show_runtime_servers_on_startup())

    sweep.assert_called_once()


def test_shutdown_handler_stops_tracked_child_and_sweeps_strays():
    from vibe import ui_server

    with patch("core.show_runtime.stop_show_runtime_manager") as stop, patch(
        "core.show_runtime.sweep_orphan_show_runtime_servers"
    ) as sweep:
        ui_server.stop_show_runtime_on_shutdown()

    stop.assert_called_once()
    sweep.assert_called_once()
