from pathlib import Path

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
