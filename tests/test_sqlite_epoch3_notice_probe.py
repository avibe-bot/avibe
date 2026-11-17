"""The Incus notice probe, exercised on a small local fixture of the same shape."""
from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest

from storage.importer import ensure_sqlite_state

PROBE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "sqlite_epoch3_notice_probe.py"))


@pytest.fixture
def fixture_root(monkeypatch, tmp_path):
    root = tmp_path / "fixture"
    (root / "home").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("AVIBE_HOME", str(root / "avibe"))
    ensure_sqlite_state()
    return root


def _mark_backup_skipped() -> None:
    from storage.db import get_cached_sqlite_engine
    from storage.models import state_meta

    with get_cached_sqlite_engine().begin() as conn:
        conn.execute(state_meta.insert().values(
            key="storage.epoch3_backup_skipped",
            value_json=json.dumps({"converted_at": "2026-11-16T07:00:00Z", "database_bytes": 12_345}),
            updated_at="2026-11-16T07:00:00Z",
        ))


def test_probe_passes_after_a_no_copy_conversion(fixture_root, capsys):
    _mark_backup_skipped()

    assert PROBE["main"](["--root", str(fixture_root), "--language", "zh", "--repeat", "3"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and all(result["checks"].values())
    assert result["delivered"] == [True, False, False]
    assert result["record_after"]["database_bytes"] == 12_345


def test_probe_fails_without_the_conversion_record(fixture_root, capsys):
    assert PROBE["main"](["--root", str(fixture_root)]) == 1

    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["record_present_before"] is False and checks["one_notice"] is False


def test_probe_refuses_outside_its_isolated_environment(fixture_root, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(fixture_root / "elsewhere"))
    with pytest.raises(RuntimeError, match="isolated HOME/AVIBE_HOME"):
        PROBE["main"](["--root", str(fixture_root)])


@pytest.mark.parametrize("confirmed", [True, False])
def test_disk_full_outcome_reports_what_vibe_status_shows(fixture_root, confirmed):
    from storage.migrations import DatabaseDiskFullError
    from vibe import runtime

    outcome = PROBE["disk_full_outcome"](fixture_root, DatabaseDiskFullError(rollback_confirmed=confirmed), clear=True)

    detail = runtime.DATABASE_DISK_FULL_DETAIL if confirmed else runtime.DATABASE_DISK_FULL_UNCONFIRMED_DETAIL
    assert outcome["code"] == "database_disk_full" and outcome["detail"] == detail
    assert outcome["status"] == {"state": "error", "detail": detail, "code": "database_disk_full"}
    assert outcome["desktop_reason"] == ("database_disk_full" if confirmed else "database_disk_full_unconfirmed")
    assert outcome["ok"] is True
    assert runtime.service_start_failure() is None  # cleared after reading


def test_disk_full_outcome_takes_only_the_caught_error(fixture_root):
    with pytest.raises(TypeError, match="DatabaseDiskFullError"):
        PROBE["disk_full_outcome"](fixture_root, RuntimeError("disk full"))
