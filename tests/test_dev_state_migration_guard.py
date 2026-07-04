from __future__ import annotations

from pathlib import Path

import pytest

from config import paths
from core import chat_discovery
from core.vibe_agents import VibeAgentStore
from storage import migrations
from storage.background import SQLiteBackgroundTaskStore
from storage.migrations import UnsafeDefaultStateMigrationError
from vibe import cli, restart_supervisor, runtime


def _set_default_home_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    db_path = home / ".avibe" / "state" / "vibe.sqlite"
    monkeypatch.setattr(migrations.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(paths.AVIBE_HOME_ENV, raising=False)
    monkeypatch.delenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, raising=False)
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)
    return db_path


def test_chat_discovery_guard_blocks_default_state_before_mkdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        chat_discovery._ensure_sqlite()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_background_store_guard_blocks_default_state_before_mkdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        SQLiteBackgroundTaskStore()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_vibe_agent_store_guard_blocks_default_state_before_mkdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        VibeAgentStore()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_cli_start_guard_blocks_default_state_before_data_dirs(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        cli.cmd_start()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_runtime_lock_guard_blocks_default_state_before_mkdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        runtime.acquire_service_instance_lock()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_runtime_start_service_guard_blocks_default_state_before_mkdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        runtime.start_service(wait_for_ready=False)

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_restart_schedule_guard_blocks_default_state_before_status(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        restart_supervisor.schedule_restart(delay_seconds=60, vibe_path="/bin/vibe", trigger="test")

    assert not db_path.exists()
    assert not db_path.parent.exists()
    assert not (db_path.parents[1] / "runtime").exists()
    assert not (db_path.parents[1] / "logs").exists()


def test_restart_job_guard_blocks_before_stopping_runtime(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)
    stopped = False

    def stop_runtime(*, stop_ui: bool = True):
        nonlocal stopped
        stopped = True
        return True, {}, 0.0, None, True, 0.0

    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", stop_runtime)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        restart_supervisor._run_restart_job(job_id="jobabc", delay_seconds=0, vibe_path="/bin/vibe", trigger="test")

    assert stopped is False
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_runtime_ensure_dirs_guard_blocks_legacy_home_before_migration(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)
    home = db_path.parents[2]
    legacy_home = home / ".vibe_remote"
    legacy_state = legacy_home / "state"
    legacy_state.mkdir(parents=True)
    (legacy_state / "settings.json").write_text('{"ok":true}', encoding="utf-8")

    with pytest.raises(UnsafeDefaultStateMigrationError):
        runtime.ensure_dirs()

    assert legacy_home.is_dir()
    assert not (home / ".avibe").exists()


def test_paths_ensure_data_dirs_guard_blocks_legacy_home_before_migration(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)
    home = db_path.parents[2]
    legacy_home = home / ".vibe_remote"
    legacy_state = legacy_home / "state"
    legacy_state.mkdir(parents=True)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        paths.ensure_data_dirs()

    assert legacy_home.is_dir()
    assert not (home / ".avibe").exists()


def test_runtime_ensure_config_guard_blocks_before_default_workdir(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)
    home = db_path.parents[2]

    with pytest.raises(UnsafeDefaultStateMigrationError):
        runtime.ensure_config()

    assert not (home / "work").exists()
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_regression_deployment_env_alone_does_not_bypass_default_state_guard(monkeypatch, tmp_path: Path) -> None:
    db_path = _set_default_home_guard(monkeypatch, tmp_path)
    monkeypatch.setenv("VIBE_DEPLOYMENT_ENV", "regression")

    with pytest.raises(UnsafeDefaultStateMigrationError):
        migrations.guard_source_checkout_default_state_migration(db_path)
