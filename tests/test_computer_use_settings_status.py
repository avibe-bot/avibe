"""Settings reads saved opt-in, never readiness, and never repairs a state file."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import computer_use as cu
from vibe import internal_client, ui_server


def saved_record(tmp_path, enabled):
    return cu.parse_computer_use_state({
        "schema_version": 1, "enabled": enabled,
        "state": "needs_runtime" if enabled else "off",
        "reason": "runtime_unavailable" if enabled else None,
        "shell_pid": 123, "instance_id": "saved-shell", "generation": 1,
        "driver_version": "0.31.0",
        "tool_snapshot": {"path": str(tmp_path / "tools.json"), "sha256": "0" * 64},
    })


@pytest.mark.parametrize("phase,reason", [
    ("ready", None), ("error", "health_failed"), ("needs_runtime", "runtime_unavailable"),
    ("unavailable", "snapshot_invalid"), ("unavailable", "shell_not_running"),
])
@pytest.mark.parametrize("enabled", [False, True])
def test_api_opt_in_survives_readiness_and_controller_failures(monkeypatch, tmp_path, phase, reason, enabled):
    record = saved_record(tmp_path, enabled)
    monkeypatch.setattr(ui_server, "_desktop_computer_use_supported", lambda: True)
    monkeypatch.setattr(ui_server, "effective_computer_use_status", lambda: cu.ComputerUseStatus(phase, reason, record))

    async def absent_controller():
        return None

    monkeypatch.setattr(internal_client, "health_identity", absent_controller)
    response = ui_server.app.test_client().get("/api/desktop/computer-use/status")
    assert response.status_code == 200
    assert response.get_json()["enabled"] is enabled
    assert response.headers["Cache-Control"] == "no-store"
    if phase == "ready":
        assert response.get_json()["reason"] == "controller_unavailable"
    # Public status must not leak local paths or the full native record.
    assert set(response.get_json()) == {"supported", "status", "reason", "enabled", "host_platform"}


@pytest.mark.parametrize("reason", ["invalid_state_file", "state_unverified"])
@pytest.mark.parametrize("enabled", [False, True])
def test_unverified_status_never_exposes_opt_in(tmp_path, reason, enabled):
    status = cu.ComputerUseStatus("unavailable", reason, saved_record(tmp_path, enabled))
    assert status.enabled is None
    assert status.as_dict()["enabled"] is None


@pytest.mark.parametrize("contents", ["{", '{"schema_version":2}', '{"schema_version":1}'])
def test_unknown_old_or_damaged_state_is_not_off_and_is_not_rewritten(tmp_path, contents):
    path = tmp_path / "computer-use.json"
    path.write_text(contents)
    status = cu.effective_computer_use_status(state_path=path)
    assert status.enabled is None
    assert status.reason == "invalid_state_file"
    assert path.read_text() == contents


def test_first_install_is_off_but_unreadable_state_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "computer-use.json"
    assert cu.effective_computer_use_status(state_path=path).enabled is False
    path.mkdir()  # an existing unreadable/non-file artifact is not first install
    assert cu.effective_computer_use_status(state_path=path).enabled is None
    assert path.is_dir()


@pytest.mark.parametrize("enabled", [False, True])
def test_windows_untrusted_opened_state_is_unknown_even_when_off(tmp_path, monkeypatch, enabled):
    # Exercise the production trusted-reader error path, not a phase inference.
    from core import computer_windows

    path = tmp_path / "computer-use.json"
    path.write_text(json.dumps({"enabled": enabled}))

    def refuse(_path):
        raise PermissionError("untrusted opened object")

    monkeypatch.setattr(cu, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(computer_windows, "read_state_text", refuse)
    assert cu.effective_computer_use_status(state_path=path).enabled is None
    assert json.loads(path.read_text())["enabled"] is enabled
