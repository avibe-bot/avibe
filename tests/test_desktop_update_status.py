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
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "remote_install_allowed": False,
        "remote_request": None,
    }


def test_remote_status_is_relayed_and_bad_remote_fields_degrade_independently() -> None:
    payload = {
        "schema_version": 1,
        "latest": "3.2.4-rc.1",
        "has_update": True,
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
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "remote_install_allowed": True,
        "remote_request": payload["remote_request"],
    }

    payload["remote_install_allowed"] = "yes"
    payload["remote_request"] = {**payload["remote_request"], "progress": True}
    assert parse_desktop_update_status(payload) == {
        "latest": "3.2.4-rc.1",
        "has_update": True,
        "remote_install_allowed": False,
        "remote_request": None,
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
