from pathlib import Path

import pytest

from core.computer_use import COMPUTER_USE_STATE_DIR_ENV
from vibe.desktop_update_status import (
    desktop_update_status_path,
    parse_desktop_update_status,
)


def test_desktop_update_status_path_ignores_avibe_home(tmp_path: Path) -> None:
    """A Runtime the shell adopted finds the shell's status under any AVIBE_HOME."""

    env = {"AVIBE_HOME": str(tmp_path / "runtime-home")}
    assert desktop_update_status_path(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "user" / "Library" / "Application Support" / "bot.avibe.desktop" / "update-status.json"
    env[COMPUTER_USE_STATE_DIR_ENV] = str(tmp_path / "isolated-desktop")
    assert desktop_update_status_path(
        environ=env,
        home=tmp_path / "user",
        platform="darwin",
    ) == tmp_path / "isolated-desktop" / "update-status.json"


def test_old_status_defaults_remote_fields_without_losing_update() -> None:
    assert parse_desktop_update_status(
        {
            "schema_version": 1,
            "latest": "3.2.4-rc.1",
            "has_update": True,
        }
    ) == {
        "phase": "available",
        "desktop_version": None,
        "prereleases": None,
        "checked_at": None,
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "auto_update": None,
        "remote_install_allowed": False,
        "remote_request": None,
        "host_actions": [],
        "control_request": None,
    }


CHECK_RECORD = {
    "schema_version": 1,
    "phase": "error",
    "current": "3.2.5",
    "latest": None,
    "channel": "test",
    "has_update": False,
    "checked_at": "2026-11-13T06:34:00Z",
}


def test_the_check_result_version_channel_and_time_are_relayed() -> None:
    parsed = parse_desktop_update_status(CHECK_RECORD)

    assert parsed is not None
    assert {key: parsed[key] for key in ("phase", "desktop_version", "prereleases", "checked_at")} == {
        "phase": "error",
        "desktop_version": "3.2.5",
        "prereleases": True,
        "checked_at": "2026-11-13T06:34:00Z",
    }


@pytest.mark.parametrize(
    ("changes", "unknown"),
    [
        pytest.param({"phase": "checking"}, "phase", id="in-flight-phase"),
        pytest.param({"phase": "available"}, "phase", id="available-without-has-update"),
        pytest.param({"phase": 3}, "phase", id="non-string-phase"),
        pytest.param({"current": ""}, "desktop_version", id="blank-version"),
        pytest.param({"current": ["3.2.5"]}, "desktop_version", id="non-string-version"),
        pytest.param({"channel": "beta"}, "prereleases", id="unknown-channel"),
        pytest.param({"channel": None}, "prereleases", id="null-channel"),
        pytest.param({"checked_at": "yesterday"}, "checked_at", id="unparsable-time"),
        pytest.param({"checked_at": "2026-11-13T06:34:00"}, "checked_at", id="time-without-zone"),
    ],
)
def test_an_unusable_check_field_reads_as_unknown_and_keeps_the_rest(changes: dict, unknown: str) -> None:
    parsed = parse_desktop_update_status({**CHECK_RECORD, **changes})

    assert parsed is not None
    assert parsed[unknown] is None
    assert {key: value for key, value in parsed.items() if key != unknown and value is not None} == {
        key: value
        for key, value in {
            "phase": "error",
            "desktop_version": "3.2.5",
            "prereleases": True,
            "checked_at": "2026-11-13T06:34:00Z",
            "has_update": False,
            "remote_install_allowed": False,
            "host_actions": [],
        }.items()
        if key != unknown
    }


def test_remote_status_is_relayed_and_bad_remote_fields_degrade_independently() -> None:
    payload = {
        "schema_version": 1,
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "auto_update": None,
        "remote_install_allowed": True,
        "remote_request": {
            "request_id": "request-1",
            "target": "3.2.4-rc.1",
            "phase": "downloading",
            "progress": 42,
            "error_code": None,
            "updated_at": "2026-10-29T02:00:00Z",
        },
    }
    assert parse_desktop_update_status(payload) == {
        "phase": "available",
        "desktop_version": None,
        "prereleases": None,
        "checked_at": None,
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "auto_update": None,
        "remote_install_allowed": True,
        "remote_request": payload["remote_request"],
        "host_actions": [],
        "control_request": None,
    }

    payload["remote_install_allowed"] = "yes"
    payload["remote_request"] = {**payload["remote_request"], "progress": True}
    assert parse_desktop_update_status(payload) == {
        "phase": "available",
        "desktop_version": None,
        "prereleases": None,
        "checked_at": None,
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "auto_update": None,
        "remote_install_allowed": False,
        "remote_request": None,
        "host_actions": [],
        "control_request": None,
    }


@pytest.mark.parametrize(
    ("field", "value", "kept"),
    [
        pytest.param("request_id", [], False, id="request-id-list"),
        pytest.param("request_id", {}, False, id="request-id-object"),
        pytest.param("request_id", 7, False, id="request-id-number"),
        pytest.param("request_id", None, False, id="request-id-null"),
        pytest.param("request_id", "opaque-request", True, id="request-id-opaque-string"),
        pytest.param("target", [], False, id="target-list"),
        pytest.param("target", {}, False, id="target-object"),
        pytest.param("target", 7, False, id="target-number"),
        pytest.param("target", None, False, id="target-null"),
        pytest.param("target", "opaque-target", True, id="target-opaque-string"),
        pytest.param("phase", [], False, id="phase-list"),
        pytest.param("phase", {}, False, id="phase-object"),
        pytest.param("phase", 7, False, id="phase-number"),
        pytest.param("phase", None, False, id="phase-null"),
        pytest.param("phase", "unknown", False, id="phase-unknown-string"),
        pytest.param("progress", [], False, id="progress-list"),
        pytest.param("progress", {}, False, id="progress-object"),
        pytest.param("progress", 101, False, id="progress-out-of-range-number"),
        pytest.param("progress", None, True, id="progress-null"),
        pytest.param("progress", "unknown", False, id="progress-string"),
        pytest.param("error_code", [], False, id="error-code-list"),
        pytest.param("error_code", {}, False, id="error-code-object"),
        pytest.param("error_code", 7, False, id="error-code-number"),
        pytest.param("error_code", None, True, id="error-code-null"),
        pytest.param("error_code", "unknown", True, id="error-code-opaque-string"),
        pytest.param("updated_at", [], False, id="updated-at-list"),
        pytest.param("updated_at", {}, False, id="updated-at-object"),
        pytest.param("updated_at", 7, False, id="updated-at-number"),
        pytest.param("updated_at", None, False, id="updated-at-null"),
        pytest.param("updated_at", "unknown", False, id="updated-at-unknown-string"),
    ],
)
def test_every_remote_status_field_degrades_without_losing_the_ordinary_result(
    field: str,
    value: object,
    kept: bool,
) -> None:
    remote_request = {
        "request_id": "request-1",
        "target": "3.2.4-rc.1",
        "phase": "downloading",
        "progress": 42,
        "error_code": None,
        "updated_at": "2026-10-29T02:00:00Z",
    }
    remote_request[field] = value
    parsed = parse_desktop_update_status(
        {
            "schema_version": 1,
            "latest": "3.2.4-rc.1",
            "has_update": True,
            "remote_install_allowed": True,
            "remote_request": remote_request,
        }
    )

    assert parsed is not None
    assert parsed["latest"] == "3.2.4-rc.1"
    assert parsed["has_update"] is True
    assert parsed["remote_install_allowed"] is True
    assert (parsed["remote_request"] is not None) is kept
