"""Settings reads saved opt-in, never readiness, and never repairs a state file."""
from __future__ import annotations

import json
import os
import sys
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


@pytest.fixture
def windows_opened_reader(monkeypatch):
    from core import computer_windows

    opens = []

    def create(name, access, sharing, security, disposition, flags, template):
        opens.append((access, sharing, disposition, flags))
        return os.open(name, os.O_RDONLY)

    kernel = SimpleNamespace(CreateFileW=create, CloseHandle=os.close)
    monkeypatch.setattr(computer_windows, "windows_security", lambda: SimpleNamespace(kernel32=kernel))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: handle))
    monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
    monkeypatch.setattr(cu, "os", SimpleNamespace(name="nt"))
    return opens


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("verdicts", [(False,), (True, False)])
def test_windows_readable_but_untrusted_opened_state_is_unknown(
    tmp_path, monkeypatch, enabled, verdicts, windows_opened_reader,
):
    path = tmp_path / "computer-use.json"
    path.write_text(json.dumps({"enabled": enabled}))
    checked = []
    verdict = iter(verdicts)

    def verify(_path, *, fd=None):
        assert fd is not None, "trust must be checked on the opened record"
        assert os.fstat(fd).st_size > 0
        checked.append(fd)
        return next(verdict)

    monkeypatch.setattr(cu, "_windows_private_artifact_is_safe", verify)
    status = cu.effective_computer_use_status(state_path=path)
    assert (status.reason, status.enabled) == ("state_unverified", None)
    assert len(checked) == len(verdicts)
    assert len(set(checked)) == 1
    with pytest.raises(OSError):
        os.fstat(checked[0])
    assert windows_opened_reader == [(0x80000000, 7, 3, 0x00200080)]
    assert json.loads(path.read_text())["enabled"] is enabled


@pytest.mark.skipif(os.name == "nt", reason="portable descriptor/path-race simulation; native sharing is tested separately")
def test_windows_reader_validates_same_open_object_across_path_replacement(
    tmp_path, monkeypatch, windows_opened_reader,
):
    from core.computer_windows import read_state_text

    path = tmp_path / "computer-use.json"
    path.write_text("trusted original bytes")
    identity = path.stat().st_ino
    checked = []

    def verify(_path, *, fd=None):
        assert fd is not None
        checked.append(os.fstat(fd).st_ino)
        if len(checked) == 1:
            replacement = tmp_path / "replacement"
            replacement.write_text("untrusted replacement bytes")
            os.replace(replacement, path)
        return os.fstat(fd).st_ino == identity

    monkeypatch.setattr(cu, "_windows_private_artifact_is_safe", verify)
    assert read_state_text(path) == "trusted original bytes"
    assert checked == [identity, identity]
    assert path.read_text() == "untrusted replacement bytes"
