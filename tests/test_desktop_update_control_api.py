"""Real HTTP guards and control delivery; native execution has its own Rust tests.

The install API tests cannot catch misclassified new routes, a control command
writing the legacy mailbox, or standalone channel changes replacing other config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from config.v2_config import V2Config
from tests.ui_server_test_helpers import _save_config, csrf_headers, remote_peer, remote_session_cookie
from vibe import api, remote_access
from vibe.desktop_update_control import read_control_request
from vibe.ui_server import app


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path))
    payload = {
        "schema_version": 1, "current": "3.2.6", "latest": "3.2.7", "has_update": True,
        "phase": "available", "channel": "stable", "checked_at": "2026-11-13T08:00:00Z",
        "host_actions": ["check", "prereleases"],
    }
    status = tmp_path / "update-status.json"
    status.write_text(json.dumps(payload))
    return tmp_path, status, payload


def post(client, action="check", payload=None, **kwargs):
    return client.post(
        f"/api/update/{action}", json={} if payload is None else payload,
        headers=csrf_headers(client, "http://127.0.0.1"), base_url="http://127.0.0.1", **kwargs,
    )


@pytest.mark.parametrize("action,payload", [("check", {}), ("prereleases", {"enabled": True})])
@pytest.mark.parametrize("role,expected", [("owner", 409), ("member", 409), ("editor", 403), ("viewer", 403)])
def test_controls_keep_the_existing_instance_manager_role(monkeypatch, tmp_path, action, payload, role, expected):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path / "desktop"))
    config = _save_config(tmp_path, paired=True)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(config, f"{role}@example.com", f"{role}-1", role=role,
                              access_source="owner" if role == "owner" else "email"),
        domain="alex.avibe.bot",
    )
    response = client.post(
        f"/api/update/{action}", json=payload, headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot", environ_base=remote_peer(),
    )
    assert response.status_code == expected
    assert response.get_json()["error"] == (
        "instance_access_forbidden" if expected == 403 else "desktop_update_action_unsupported"
    )
    assert not (tmp_path / "desktop" / "desktop-update-control.json").exists()


@pytest.mark.parametrize("action,payload", [("check", {}), ("prereleases", {"enabled": True})])
def test_controls_require_csrf_and_same_origin(desktop, action, payload):
    root, _, _ = desktop
    client = app.test_client()
    missing = client.post(f"/api/update/{action}", json=payload, headers={"Origin": "http://127.0.0.1"})
    assert missing.status_code == 403
    assert missing.get_json()["message"] == "Forbidden: invalid csrf token"
    invalid = client.post(
        f"/api/update/{action}", json=payload,
        headers={**csrf_headers(client, "http://127.0.0.1"), "Origin": "https://attacker.example"},
        base_url="http://127.0.0.1",
    )
    assert invalid.status_code == 403
    assert invalid.get_json()["message"] == "Forbidden: invalid origin"
    assert not (root / "desktop-update-control.json").exists()


@pytest.mark.parametrize("action,payload", [
    ("check", {"enabled": True}), ("check", []), ("prereleases", {}),
    ("prereleases", {"enabled": 1}), ("prereleases", {"enabled": "true"}),
    ("prereleases", {"enabled": False, "target": "3.2.7"}),
])
def test_controls_reject_ambiguous_payloads(desktop, action, payload):
    root, _, _ = desktop
    response = post(app.test_client(), action, payload)
    assert response.status_code == 422
    assert response.get_json()["error"] == "invalid_request"
    assert not (root / "desktop-update-control.json").exists()


@pytest.mark.parametrize("action,enabled", [("check", None), ("prereleases", False), ("prereleases", True)])
@pytest.mark.parametrize("phase,code,expected", [("checking", None, 200), ("completed", None, 200),
                                               ("failed", "check_failed", 409), ("expired", "expired", 409)])
def test_control_http_delivery_and_receipt_relay(monkeypatch, desktop, action, enabled, phase, code, expected):
    root, status, base = desktop
    events = []
    def shell_receives(event, body):
        events.append(event)
        req = read_control_request(root / "desktop-update-control.json")
        assert req["request_id"] == body["request_id"]
        receipt = {key: req[key] for key in ("request_id", "action", "created_at", "expires_at")}
        receipt.update(enabled=req.get("enabled"), phase=phase, error_code=code, updated_at=req["created_at"])
        status.write_text(json.dumps({**base, "control_request": receipt}))
    monkeypatch.setattr("vibe.sse_broker.broker.publish", shell_receives)
    response = post(app.test_client(), action, {} if enabled is None else {"enabled": enabled})
    assert response.status_code == expected
    body = response.get_json()
    assert body["ok"] is (expected == 200)
    assert body["request_id"] == read_control_request(root / "desktop-update-control.json")["request_id"]
    if code:
        assert body["error"] == code
    assert events == ["desktop.update.requested"]
    assert not (root / "desktop-update-request.json").exists()
    assert not (root / "update-channel.json").exists()
    snapshot = api.get_version_info()
    assert snapshot["control_request"]["phase"] == phase
    assert snapshot["phase"] == ("checking" if phase == "checking" else "available")
    assert snapshot["latest"] == "3.2.7"


def test_control_ack_timeout_expires_only_the_submitted_identity(monkeypatch, desktop):
    root, _, _ = desktop
    monkeypatch.setattr("vibe.ui_server._DESKTOP_UPDATE_ACK_TIMEOUT_SECONDS", 0)
    response = post(app.test_client())
    assert response.status_code == 503
    assert response.get_json()["error"] == "desktop_shell_unavailable"
    req = read_control_request(root / "desktop-update-control.json")
    assert req["state"] == "expired" and req["request_id"] == response.get_json()["request_id"]


def test_legacy_shell_capability_absence_does_not_change_any_mailbox(desktop):
    root, status, base = desktop
    base.pop("host_actions")
    status.write_text(json.dumps(base))
    response = post(app.test_client())
    assert response.status_code == 409
    assert response.get_json()["error"] == "desktop_update_action_unsupported"
    assert not (root / "desktop-update-control.json").exists()
    assert not (root / "desktop-update-request.json").exists()


def test_standalone_channel_is_transactional_and_checks_the_updated_preference(monkeypatch, tmp_path):
    monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path)
    config.language = "zh"
    config.update.prereleases = False
    config.save()
    before = json.loads((tmp_path / "config" / "config.json").read_text())
    observed = []
    def check():
        observed.append(V2Config.load().update.prereleases)
        return {"current": "3.2.6", "latest": "3.2.7rc1", "has_update": True, "error": None}
    monkeypatch.setattr(api, "get_version_info", check)
    client = app.test_client()
    response = post(client, "prereleases", {"enabled": True})
    assert response.status_code == 200 and response.get_json()["snapshot"]["latest"] == "3.2.7rc1"
    after = json.loads((tmp_path / "config" / "config.json").read_text())
    before["update"]["prereleases"] = True
    assert before == after
    assert post(client).status_code == 200
    assert observed == [True, True]  # each explicit check calls the owner afresh


def test_standalone_check_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    monkeypatch.setattr(api, "get_version_info", lambda: {"error": "network unavailable"})
    response = post(app.test_client())
    assert response.status_code == 502
    assert response.get_json()["error"] == "update_check_failed"
