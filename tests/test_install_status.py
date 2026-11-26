"""Standalone version reads follow the actual installer, not the client that started it."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from config.v2_config import UpdateConfig
from core.update_checker import UpdateChecker
from vibe import api, cli, restart_supervisor, runtime, upgrade
from vibe.ui_server import app


@pytest.fixture
def standalone(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    monkeypatch.delenv("VIBE_BUILD_METADATA_PATH", raising=False)
    # An automatic install takes exactly its target: a published release newer than the running one.
    monkeypatch.setattr("vibe.__version__", "1.0", raising=False)
    metadata = tmp_path / "pypi.json"
    metadata.write_text('{"releases": {"1.0": [{}], "2.0": [{}]}}', encoding="utf-8")
    monkeypatch.setenv("VIBE_UPDATE_METADATA_URL", metadata.as_uri())
    monkeypatch.setattr(api, "get_latest_version_info", lambda *a, **kw: {
        "current": "1.0", "latest": "2.0", "has_update": True, "error": None,
    })
    return tmp_path


def version_install():
    response = app.test_client().get("/api/version", base_url="http://127.0.0.1")
    assert response.status_code == 200
    return response.get_json()["install"]


@pytest.mark.parametrize("installer", ["web", "automatic", "cli"])
def test_other_client_sees_install_until_its_restart_finishes(standalone, monkeypatch, installer):
    # The CLI can attempt an upgrade even if the metadata lookup failed.
    target = None if installer == "cli" else "2.0"
    monkeypatch.setattr(cli, "get_latest_version", lambda: {"latest": target, "error": "offline"})
    monkeypatch.setattr(api, "get_running_vibe_path", lambda: None)
    monkeypatch.setattr(cli, "cache_running_vibe_path", lambda: None)
    for owner in (api, cli):
        monkeypatch.setattr(owner, "_runtime_process_was_running", lambda: True)
    entered, release = threading.Event(), threading.Event()

    def stage(plan, **kwargs):
        entered.set()
        assert release.wait(8), "the version read waited for the installer"
        return subprocess.CompletedProcess(plan.command, 0, "done", "")

    for owner in (api, upgrade):
        monkeypatch.setattr(owner, "build_upgrade_plan", lambda **kw: upgrade.UpgradePlan(["test"], None, "uv"))
        monkeypatch.setattr(owner, "execute_upgrade_plan", stage)
    # Keep the real restart seed and status policy. Never spawn a supervisor.
    monkeypatch.setattr(restart_supervisor, "outside_desktop_session", lambda: False)
    monkeypatch.setattr(restart_supervisor, "subprocess", SimpleNamespace(
        STDOUT=subprocess.STDOUT, Popen=lambda *a, **kw: SimpleNamespace(pid=123),
    ))
    monkeypatch.setattr(restart_supervisor, "get_restart_invocation_command", lambda **kw: ["test", "restart"])
    monkeypatch.setattr(restart_supervisor, "get_restart_environment", lambda **kw: {})

    def run():
        if installer == "cli":
            assert cli.cmd_upgrade() == 0
        elif installer == "automatic":
            checker = UpdateChecker(None, UpdateConfig())
            assert asyncio.run(checker._perform_update("2.0"))["restarting"]
        else:
            assert api.do_upgrade()["restarting"]

    assert version_install() is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(run)
        try:
            assert entered.wait(5)
            installing = version_install()
            started = installing["started_at"]
            assert datetime.fromisoformat(started.replace("Z", "+00:00")).utcoffset() == timedelta(0)
            assert installing == {"phase": "installing", "target": target, "started_at": started}
        finally:
            release.set()
        worker.result()
    assert version_install() == {**installing, "phase": "restarting"}
    path = runtime.get_restart_status_path()
    job = runtime.read_json(path)
    runtime.write_json(path, {**job, "state": "succeeded"})
    assert version_install() is None
    runtime.write_json(path, {**job, "job_id": "later-unrelated-restart"})
    assert version_install() is None


@pytest.mark.parametrize("crash", [False, True], ids=["install-fails", "installer-dies"])
def test_no_install_after_failure_or_holder_death(standalone, crash):
    # Exercise the actual OS release on process death, bypassing Python cleanup.
    code = '''
import os, subprocess, sys
from vibe import api, upgrade
api.get_running_vibe_path = lambda: None
api.get_version_info = lambda: {"latest": "2.0", "has_update": True}
api.exact_release_spec = lambda _version: "avibe-os"
api.has_newer_version = lambda _candidate, _current: True
api.build_upgrade_plan = lambda **kw: upgrade.UpgradePlan(["test"], None, "uv")
api._runtime_process_was_running = lambda: False
def stage(plan, **kw):
    assert upgrade.get_install_status()["phase"] == "installing"
    if sys.argv[1] == "crash":
        os._exit(17)
    return subprocess.CompletedProcess(plan.command, 1, "", "failed")
api.execute_upgrade_plan = stage
assert not api.do_upgrade()["ok"]
'''
    proc = subprocess.run(
        [sys.executable, "-c", code, "crash" if crash else "fail"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "AVIBE_HOME": str(standalone)},
        timeout=30,
    )
    assert proc.returncode == (17 if crash else 0)
    assert version_install() is None
    with upgrade.atomic_upgrade_lock():  # Launcher maintenance cannot resurrect an install.
        assert version_install() is None
