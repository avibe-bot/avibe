"""Long migrations are progress, never proof of readiness or infinite waits."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import main as service_main
from vibe import runtime


@pytest.mark.parametrize("fails", [False, True])
def test_database_preparation_publishes_phase_and_clears_it_before_controller(tmp_path, monkeypatch, fails):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    runtime.acquire_service_instance_lock()
    try:
        def prepare(**kwargs):
            assert runtime.read_service_instance_lock_record()["phase"] == "migrating"
            assert runtime.service_instance_migrating(os.getpid())
            assert not runtime.service_instance_started(os.getpid())
            assert runtime.service_instance_still_starting(os.getpid())
            status = json.loads(runtime.render_status())
            assert status["state"] == "starting"
            assert not status["running"]
            assert "migration in progress" in status["detail"]
            if fails:
                raise RuntimeError("migration failed")
            return "prepared"

        monkeypatch.setattr("storage.importer.repair_sqlite_state", prepare)
        if fails:
            with pytest.raises(RuntimeError, match="migration failed"):
                service_main.prepare_sqlite_state(SimpleNamespace(platform="avibe"))
        else:
            assert service_main.prepare_sqlite_state(SimpleNamespace(platform="avibe")) == "prepared"
        assert not runtime.service_instance_migrating(os.getpid())
        assert runtime.service_instance_still_starting(os.getpid())
        assert not runtime.service_instance_started(os.getpid())
    finally:
        runtime.release_service_instance_lock()


@pytest.mark.parametrize(
    "case,expected_pid,elapsed",
    [
        ("slow-migration", 222, 6.0),
        ("ordinary-startup", None, 2.0),
        ("hung-migration", None, 10.0),
        ("dead-owner", None, 3.0),
        ("hung-controller-after-migration", None, 7.0),
        ("reused-pid", None, 2.0),
        ("different-pid", None, 2.0),
    ],
)
def test_readiness_budget_follows_verified_migration_phase(monkeypatch, caplog, case, expected_pid, elapsed):
    # Virtual time pins the boundary without sleeping minutes. Actual record
    # interpretation stays real; only the process/clock observations are faked.
    clock = [0.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 0.5))
    monkeypatch.setattr(runtime, "SERVICE_MIGRATION_TIMEOUT_SECONDS", 10.0, raising=False)
    monkeypatch.setattr(runtime, "_wait_for_scoped_service_pid", lambda pid, timeout: 222)
    monkeypatch.setattr(runtime, "process_start_token", lambda pid: "this-process")
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: not (case == "dead-owner" and clock[0] >= 3))

    def record():
        phase = "migrating"
        if case == "ordinary-startup" or (
            case in {"slow-migration", "hung-controller-after-migration"} and clock[0] >= 5
        ):
            phase = "starting"
        if case == "slow-migration" and clock[0] >= 6:
            phase = "running"
        return {
            "pid": 333 if case == "different-pid" else 222,
            "start_token": "old-process" if case == "reused-pid" else "this-process",
            "phase": phase,
        }

    monkeypatch.setattr(runtime, "read_service_instance_lock_record", record)
    with caplog.at_level("INFO", logger="vibe.runtime"):
        assert runtime.wait_for_service_ready(111, timeout=2) == expected_pid
    assert clock[0] == elapsed
    progress = [record.message for record in caplog.records if record.name == "vibe.runtime"]
    if case in {"ordinary-startup", "reused-pid", "different-pid"}:
        assert not progress
    else:
        assert "migration in progress" in progress[0]
    if case in {"slow-migration", "hung-controller-after-migration"}:
        assert "migration finished" in progress[-1]
