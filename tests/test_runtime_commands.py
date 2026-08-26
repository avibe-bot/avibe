from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.runtime_commands import (
    _ERR_STALE_AGE_SECONDS,
    RuntimeCommandWatcher,
)


def _make_watcher(tmp_path: Path, handler) -> tuple[RuntimeCommandWatcher, Path]:
    """Build a watcher whose controller's auth service exposes ``handler``."""
    auth_service = SimpleNamespace(_refresh_backend_runtime=handler)
    controller = SimpleNamespace(agent_auth_service=auth_service)
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    marker = tmp_path / "restart-opencode.cmd"
    marker.write_text('{"backend":"opencode"}', encoding="utf-8")
    return watcher, marker


def test_handle_restart_success_deletes_marker_no_err(tmp_path: Path) -> None:
    """Happy path: marker deleted, no ``.err`` companion."""

    async def handler(name: str) -> None:
        assert name == "opencode"

    watcher, marker = _make_watcher(tmp_path, handler)
    asyncio.run(watcher._handle_restart("opencode", marker))

    assert not marker.exists()
    assert not (tmp_path / "restart-opencode.cmd.err").exists()


def test_handle_restart_reads_marker_metadata(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def handler(name: str) -> None:
        assert name == "codex"

    auth_service = SimpleNamespace(_refresh_backend_runtime=handler)
    controller = SimpleNamespace(agent_auth_service=auth_service)
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    marker = tmp_path / "restart-codex.abc123.cmd"
    marker.write_text(
        '{"backend":"codex","metadata":{"reason":"manual_backend_restart","route":"/api/backend/codex/restart"}}',
        encoding="utf-8",
    )

    caplog.set_level("INFO")
    asyncio.run(watcher._handle_restart("codex", marker))

    assert not marker.exists()
    assert any("manual_backend_restart" in record.message for record in caplog.records)


def test_handle_restart_failure_writes_err_sentinel(tmp_path: Path) -> None:
    """Handler raises → marker deleted AND ``.err`` companion written."""

    async def handler(name: str) -> None:
        raise RuntimeError("transport teardown failed mid-way")

    watcher, marker = _make_watcher(tmp_path, handler)
    asyncio.run(watcher._handle_restart("opencode", marker))

    assert not marker.exists()
    err = tmp_path / "restart-opencode.cmd.err"
    assert err.exists()
    assert "transport teardown failed mid-way" in err.read_text()


def test_handle_restart_missing_handler_writes_err_sentinel(tmp_path: Path) -> None:
    """No ``_refresh_backend_runtime`` on the service → also surface failure."""
    auth_service = SimpleNamespace()  # no handler attribute
    controller = SimpleNamespace(agent_auth_service=auth_service)
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    marker = tmp_path / "restart-codex.cmd"
    marker.write_text("{}", encoding="utf-8")

    asyncio.run(watcher._handle_restart("codex", marker))

    assert not marker.exists()
    err = tmp_path / "restart-codex.cmd.err"
    assert err.exists()
    assert "refresh handler unavailable" in err.read_text()


def test_sweep_accepts_claude_restart_marker(tmp_path: Path) -> None:
    calls = []

    async def handler(name: str) -> None:
        calls.append(name)

    auth_service = SimpleNamespace(_refresh_backend_runtime=handler)
    controller = SimpleNamespace(agent_auth_service=auth_service)
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    marker = tmp_path / "restart-claude.cmd"
    marker.write_text("{}", encoding="utf-8")

    asyncio.run(watcher._sweep_once())

    assert calls == ["claude"]
    assert not marker.exists()


def test_handle_restart_err_message_truncated(tmp_path: Path) -> None:
    """Pathological multi-MB exception messages must not blow up the disk."""

    async def handler(name: str) -> None:
        raise RuntimeError("x" * 5000)

    watcher, marker = _make_watcher(tmp_path, handler)
    asyncio.run(watcher._handle_restart("opencode", marker))

    err = tmp_path / "restart-opencode.cmd.err"
    assert err.exists()
    assert len(err.read_text()) <= 1024


def test_sweep_preserves_fresh_err_companion(tmp_path: Path) -> None:
    """Regression for P2: sweep must not delete a fresh ``.err`` before the requester reads it.

    Reproduces the race where the controller writes ``<marker>.cmd.err`` to
    signal a failure, then the next sweep tick deletes it because it does
    not match ``_MARKER_PATTERN``. If the UI server's
    ``_wait_for_controller_ack`` polls *after* that sweep, it sees marker
    absent + no ``.err`` and reports success — a false positive.
    """
    controller = SimpleNamespace(agent_auth_service=SimpleNamespace())
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    err_marker = tmp_path / "restart-codex.abc123.cmd.err"
    err_marker.write_text("refresh failed", encoding="utf-8")

    asyncio.run(watcher._sweep_once())

    assert err_marker.exists(), ".err companion deleted by sweep — caller would see false success"
    assert err_marker.read_text(encoding="utf-8") == "refresh failed"


def test_sweep_evicts_orphaned_err_companion(tmp_path: Path) -> None:
    """An ``.err`` older than the stale window is safely past every caller and may be cleaned up."""
    controller = SimpleNamespace(agent_auth_service=SimpleNamespace())
    watcher = RuntimeCommandWatcher(controller, directory=tmp_path)  # type: ignore[arg-type]
    err_marker = tmp_path / "restart-codex.dead.cmd.err"
    err_marker.write_text("ancient", encoding="utf-8")
    # Backdate the mtime well past the eviction window so the sweep treats
    # it as orphaned (the original requester is long gone).
    old = time.time() - _ERR_STALE_AGE_SECONDS - 60
    os.utime(err_marker, (old, old))

    asyncio.run(watcher._sweep_once())

    assert not err_marker.exists()
