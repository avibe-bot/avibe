from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from vibe import desktop_update_control as control
from vibe.desktop_update_request import (
    DesktopUpdateRequestError,
    create_or_reuse_desktop_update_request,
    parse_desktop_update_request,
)
from vibe.desktop_update_status import desktop_update_snapshot, parse_desktop_update_status

NOW = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
TARGET = "3.2.7"


def request(action="check", **changes):
    value = {
        "schema_version": 1, "request_id": str(uuid4()), "action": action, "state": "pending",
        "created_at": control.timestamp(NOW), "expires_at": control.timestamp(NOW + control.CONTROL_REQUEST_TTL),
    }
    if action == "prereleases":
        value["enabled"] = True
    return {**value, **changes}


def receipt(req, phase="checking", **changes):
    return {
        "request_id": req["request_id"], "action": req["action"], "enabled": req.get("enabled"),
        "created_at": req["created_at"], "expires_at": req["expires_at"], "updated_at": req["created_at"],
        "phase": phase, "error_code": None, **changes,
    }


def write_status(path, **changes):
    payload = {
        "schema_version": 1, "current": "3.2.6", "latest": TARGET, "has_update": True,
        "phase": "available", "channel": "stable", "checked_at": control.timestamp(NOW),
        "remote_install_allowed": False, "remote_request": None, "host_actions": ["check", "prereleases"],
        **changes,
    }
    path.write_text(json.dumps(payload))
    return payload


def paths(tmp_path):
    status = tmp_path / "update-status.json"
    write_status(status)
    return tmp_path / control.CONTROL_REQUEST_FILE, status


def test_rust_control_fixture_is_readable_without_changing_the_legacy_offer():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "desktop_update_control.json").read_text())
    assert control.parse_control_request(fixture["request"]) == fixture["request"]
    parsed = parse_desktop_update_status(fixture["status"])
    assert parsed["control_request"] == fixture["status"]["control_request"]
    assert parsed["host_actions"] == ["check", "prereleases", "install"]
    assert parsed["has_update"] and parsed["latest"] == "3.2.7"


@pytest.mark.parametrize("action", ["check", "prereleases"])
def test_controls_can_never_parse_as_legacy_installs(action):
    value = request(action)
    assert control.parse_control_request(value) == value
    assert parse_desktop_update_request(value) is None


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 2}, {"request_id": "opaque"}, {"request_id": 2},
    {"action": "install"}, {"state": "completed"}, {"target": TARGET}, {"enabled": False},
    {"created_at": "2026-11-13T08:00:00"}, {"expires_at": "2026-11-13T08:00:00Z"},
    {"expires_at": "2026-11-13T08:02:01Z"}, {"expires_at": "yesterday"},
])
def test_invalid_control_requests_fail_closed(changes):
    assert control.parse_control_request(request(**changes)) is None


@pytest.mark.parametrize("enabled", [None, 0, 1, "true", [], {}])
def test_channel_requires_an_actual_boolean(enabled):
    assert control.parse_control_request(request("prereleases", enabled=enabled)) is None


def test_oversized_or_invalid_control_file_is_ignored(tmp_path):
    path = tmp_path / control.CONTROL_REQUEST_FILE
    path.write_bytes(b" " * (control.CONTROL_REQUEST_MAX_BYTES + 1))
    assert control.read_control_request(path) is None
    path.write_bytes(b"{broken")
    assert control.read_control_request(path) is None


def test_legacy_capability_absence_never_writes_a_request(tmp_path):
    path, status = paths(tmp_path)
    write_status(status, host_actions=None)
    with pytest.raises(DesktopUpdateRequestError, match="desktop_update_action_unsupported"):
        control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert not path.exists()


def test_pending_retry_is_idempotent_and_different_intent_is_busy(tmp_path):
    path, status = paths(tmp_path)
    first = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    again = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert again.request == first.request and again.reused and again.needs_nudge
    with pytest.raises(DesktopUpdateRequestError, match="update_in_progress"):
        control.create_or_reuse_control_request("prereleases", True, path=path, status_path=status, now=NOW)
    assert control.read_control_request(path) == first.request


def test_acknowledged_retry_does_not_nudge_and_terminal_receipt_allows_new_request(tmp_path):
    path, status = paths(tmp_path)
    first = control.create_or_reuse_control_request("prereleases", False, path=path, status_path=status, now=NOW)
    write_status(status, control_request=receipt(first.request))
    again = control.create_or_reuse_control_request("prereleases", False, path=path, status_path=status, now=NOW)
    assert again.request == first.request and again.reused and not again.needs_nudge
    assert control.acknowledged_control_request(first.request, status_path=status) is not None
    assert control.acknowledged_control_request({**first.request, "request_id": str(uuid4())}, status_path=status) is None
    assert control.acknowledged_control_request({**first.request, "enabled": True}, status_path=status) is None
    write_status(status, control_request=receipt(first.request, "completed"))
    fresh = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert not fresh.reused and fresh.request["request_id"] != first.request["request_id"]


def test_expiry_is_identity_bound_and_allows_a_new_request(tmp_path):
    path, status = paths(tmp_path)
    first = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert not control.expire_control_request(str(uuid4()), path=path)
    assert control.expire_control_request(first.request["request_id"], path=path)
    assert not control.expire_control_request(first.request["request_id"], path=path)
    fresh = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert fresh.request["request_id"] != first.request["request_id"]


@pytest.mark.parametrize("offset", [-timedelta(minutes=3), timedelta(days=365)])
def test_stale_or_future_control_receipts_cannot_lock_out_new_work(tmp_path, offset):
    path, status = paths(tmp_path)
    req = request(created_at=control.timestamp(NOW + offset),
                  expires_at=control.timestamp(NOW + offset + control.CONTROL_REQUEST_TTL))
    write_status(status, control_request=receipt(req))
    assert not control.control_in_flight(path, control.read_desktop_update_status(status), NOW)
    fresh = control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    assert fresh.request["request_id"] != req["request_id"]


@pytest.mark.parametrize("install_first", [False, True])
def test_install_and_control_mailboxes_are_mutually_exclusive_before_ack(tmp_path, install_first):
    path, status = paths(tmp_path)
    install_path = tmp_path / "desktop-update-request.json"
    def install():
        return create_or_reuse_desktop_update_request(TARGET, request_path=install_path, status_path=status, now=NOW)
    def check():
        return control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW)
    first, second = (install, check) if install_first else (check, install)
    first()
    with pytest.raises(DesktopUpdateRequestError, match="update_in_progress"):
        second()
    assert path.exists() != install_path.exists()


def test_a_terminal_install_receipt_does_not_block_controls(tmp_path):
    path, status = paths(tmp_path)
    install = create_or_reuse_desktop_update_request(
        TARGET, request_path=tmp_path / "desktop-update-request.json", status_path=status, now=NOW,
    )
    write_status(status, remote_request={
        "request_id": install.request["request_id"], "target": TARGET, "phase": "completed", "progress": None,
        "error_code": None, "updated_at": control.timestamp(NOW),
    })
    assert control.create_or_reuse_control_request("check", path=path, status_path=status, now=NOW).needs_nudge


@pytest.mark.parametrize("capabilities, expected", [
    (None, []), ("check", []), ({"check": True}, []),
    (["check", "check", "future", None, {}], ["check"]), (["prereleases", "check"], ["check", "prereleases"]),
])
def test_capabilities_degrade_without_erasing_the_offer(tmp_path, capabilities, expected):
    _, status = paths(tmp_path)
    parsed = parse_desktop_update_status(write_status(status, host_actions=capabilities))
    assert parsed["host_actions"] == expected and parsed["has_update"] and parsed["latest"] == TARGET


@pytest.mark.parametrize("changes", [
    {"request_id": "bad"}, {"action": "install"}, {"enabled": True}, {"phase": "unknown"},
    {"created_at": None}, {"expires_at": "2026-11-13T08:03:00Z"}, {"updated_at": 3}, {"error_code": {}},
])
def test_malformed_control_receipt_does_not_damage_other_fields(tmp_path, changes):
    _, status = paths(tmp_path)
    parsed = parse_desktop_update_status(write_status(status, control_request=receipt(request(), **changes)))
    assert parsed["control_request"] is None
    assert parsed["has_update"] and parsed["phase"] == "available"


def test_active_receipt_relays_checking_without_erasing_previous_offer_and_expires(tmp_path, monkeypatch):
    _, status = paths(tmp_path)
    write_status(status, control_request=receipt(request()))
    class Clock(datetime):
        moment = NOW
        @classmethod
        def now(cls, tz=None):
            return cls.moment
    monkeypatch.setattr("vibe.desktop_update_status.datetime", Clock)
    snapshot = desktop_update_snapshot("3.2.6", status)
    assert snapshot["phase"] == "checking"
    assert snapshot["latest"] == TARGET and snapshot["has_update"]
    assert snapshot["checked_at"] == control.timestamp(NOW)
    Clock.moment = NOW + timedelta(minutes=3)
    expired = desktop_update_snapshot("3.2.6", status)
    assert expired["phase"] == "available"
    assert expired["control_request"]["phase"] == "expired"
    assert expired["control_request"]["error_code"] == "expired"
