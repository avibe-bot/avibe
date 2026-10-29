from __future__ import annotations

import pytest

from tests.ui_server_test_helpers import (
    _save_config,
    csrf_headers,
    remote_peer,
    remote_session_cookie,
)
from vibe import api, remote_access
from vibe.desktop_update_request import read_desktop_update_request
from vibe.upgrade_interruptions import UpgradeInterruptions
from vibe.ui_server import app


TARGET = "3.2.4-rc.1"


def _version(*, has_update: bool = True):
    return {
        "current": "3.2.3rc11",
        "latest": TARGET if has_update else None,
        "has_update": has_update,
        "error": None,
        "managed_by": "desktop",
        "build": {"kind": "package"},
        "remote_install_allowed": False,
        "remote_request": None,
    }


def _acknowledged(*, allowed: bool = False, phase: str = "awaiting_host", error_code=None):
    remote = {
        "request_id": "",
        "target": TARGET,
        "phase": phase,
        "progress": None,
        "error_code": error_code,
        "updated_at": "2026-10-29T02:00:00Z",
    }

    async def wait(request_id: str):
        remote["request_id"] = request_id
        return (
            {
                "latest": TARGET,
                "has_update": True,
                "remote_install_allowed": allowed,
                "remote_request": remote,
            },
            remote,
        )

    return wait


@pytest.mark.parametrize(
    ("role", "expected"),
    [("owner", 409), ("member", 409), ("editor", 403), ("viewer", 403)],
)
def test_desktop_update_request_role_matrix(monkeypatch, tmp_path, role, expected) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _save_config(tmp_path, paired=True)
    client = app.test_client()
    client.set_cookie(
        remote_access.SESSION_COOKIE_NAME,
        remote_session_cookie(
            config,
            f"{role}@example.com",
            f"{role}-1",
            role=role,
            access_source="owner" if role == "owner" else "email",
        ),
        domain="alex.avibe.bot",
    )
    response = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers=csrf_headers(client, "https://alex.avibe.bot"),
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
    )
    assert response.status_code == expected
    if expected == 403:
        assert response.get_json() == {
            "ok": False,
            "error": "instance_access_forbidden",
            "required_role": "member",
        }
    else:
        assert response.get_json()["error"] == "not_desktop_managed"


def test_desktop_update_request_uses_same_origin_and_csrf_guards(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path))
    client = app.test_client()

    missing_csrf = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.get_json()["message"] == "Forbidden: invalid csrf token"

    headers = csrf_headers(client, "http://127.0.0.1")
    invalid_origin = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers={**headers, "Origin": "https://attacker.example"},
        base_url="http://127.0.0.1",
    )
    assert invalid_origin.status_code == 403
    assert invalid_origin.get_json()["message"] == "Forbidden: invalid origin"
    assert read_desktop_update_request(tmp_path / "desktop-update-request.json") is None


def test_desktop_update_request_reports_confirmation_and_delivery(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(api, "get_version_info", _version)

    async def interruptions():
        return UpgradeInterruptions(agents=1, watches=2, tasks=3)

    monkeypatch.setattr(
        "vibe.upgrade_interruptions.count_upgrade_interruptions",
        interruptions,
    )
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1")
    needs_confirmation = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers=headers,
        base_url="http://127.0.0.1",
    )
    assert needs_confirmation.status_code == 409
    assert needs_confirmation.get_json() == {
        "ok": False,
        "error": "confirmation_required",
        "counts": {
            "agents": 1,
            "watches": 2,
            "tasks": 3,
            "total": 6,
            "unknown": False,
        },
    }

    monkeypatch.setattr(
        "vibe.ui_server._wait_for_desktop_update_ack",
        _acknowledged(allowed=True),
    )
    accepted = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET, "confirm_interrupt": True},
        headers=headers,
        base_url="http://127.0.0.1",
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["delivery"] == "install"
    assert accepted.get_json()["request_id"]


@pytest.mark.parametrize(
    ("managed", "has_update", "expected", "status"),
    [
        (False, True, "not_desktop_managed", 409),
        (True, False, "no_update_available", 409),
    ],
)
def test_desktop_update_request_preconditions(
    monkeypatch,
    tmp_path,
    managed,
    has_update,
    expected,
    status,
) -> None:
    if managed:
        monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    else:
        monkeypatch.delenv("AVIBE_DESKTOP_MANAGED_RUNTIME", raising=False)
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(api, "get_version_info", lambda: _version(has_update=has_update))
    client = app.test_client()
    response = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers=csrf_headers(client, "http://127.0.0.1"),
        base_url="http://127.0.0.1",
    )
    assert response.status_code == status
    assert response.get_json()["error"] == expected


def test_desktop_update_request_expires_when_shell_does_not_ack(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_DESKTOP_MANAGED_RUNTIME", "1")
    monkeypatch.setenv("AVIBE_COMPUTER_USE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(api, "get_version_info", _version)

    async def no_interruptions():
        return UpgradeInterruptions(0, 0, 0)

    async def no_ack(_request_id):
        return None

    monkeypatch.setattr(
        "vibe.upgrade_interruptions.count_upgrade_interruptions",
        no_interruptions,
    )
    monkeypatch.setattr("vibe.ui_server._wait_for_desktop_update_ack", no_ack)
    client = app.test_client()
    response = client.post(
        "/api/desktop/update/request",
        json={"target_version": TARGET},
        headers=csrf_headers(client, "http://127.0.0.1"),
        base_url="http://127.0.0.1",
    )
    assert response.status_code == 503
    assert response.get_json()["error"] == "desktop_shell_unavailable"
    request = read_desktop_update_request(tmp_path / "desktop-update-request.json")
    assert request is not None
    assert request["state"] == "expired"


def test_upgrade_keeps_legacy_shape_and_optional_confirmation(monkeypatch) -> None:
    expected = {
        "ok": True,
        "message": "updated",
        "output": None,
        "activation_notice": None,
        "restarting": True,
    }
    calls = []
    monkeypatch.setattr(api, "do_upgrade", lambda: calls.append(True) or expected)

    async def active():
        return UpgradeInterruptions(1, 0, 0)

    monkeypatch.setattr("vibe.upgrade_interruptions.count_upgrade_interruptions", active)
    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1")

    legacy = client.post(
        "/api/upgrade",
        json={},
        headers=headers,
        base_url="http://127.0.0.1",
    )
    assert legacy.status_code == 200
    assert legacy.get_json() == expected
    assert calls == [True]

    new_client = client.post(
        "/api/upgrade",
        json={"confirm_interrupt": False},
        headers=headers,
        base_url="http://127.0.0.1",
    )
    assert new_client.status_code == 409
    assert new_client.get_json()["error"] == "confirmation_required"
    assert calls == [True]

    confirmed = client.post(
        "/api/upgrade",
        json={"confirm_interrupt": True},
        headers=headers,
        base_url="http://127.0.0.1",
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json() == expected
    assert calls == [True, True]
