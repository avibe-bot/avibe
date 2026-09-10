from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from vibe import cli
from vibe.runtime import ProcessStartInfo


def _config_with_setup_state(*, ready: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
        slack=SimpleNamespace(bot_token=""),
        has_configured_platform_credentials=lambda: ready,
    )


def _start_process(pid: int, *args, start_info: ProcessStartInfo, **kwargs) -> int:
    start_info.pid = pid
    start_info.create_unix_ms = 1789010100000.5 + pid
    start_info.reused = False
    return pid


def _assert_start_receipt(output: str) -> None:
    receipts = [line for line in output.splitlines() if line.startswith("@avibe-start-receipt:")]
    assert len(receipts) == 1
    assert json.loads(receipts[0].split(":", 1)[1]) == {
        "schema_version": 1,
        "outcome": "started",
        "service_pid": 101,
        "ui_pid": 202,
        "service_create_unix_ms": 1789010100101.5,
        "ui_create_unix_ms": 1789010100202.5,
    }


def test_cmd_vibe_marks_setup_when_no_enabled_platform_has_credentials(capsys) -> None:
    config = _config_with_setup_state(ready=False)

    with (
        patch("vibe.cli.paths.ensure_data_dirs"),
        patch("vibe.cli._ensure_config", return_value=config),
        patch("vibe.cli.runtime.stop_service") as stop_service,
        patch("vibe.cli.runtime.stop_ui") as stop_ui,
        patch("vibe.cli.runtime.start_service", side_effect=partial(_start_process, 101)),
        patch("vibe.cli.runtime.start_ui", side_effect=partial(_start_process, 202)),
        patch("vibe.cli.runtime.service_pid_recorded", return_value=True),
        patch("vibe.cli.runtime.write_status"),
        patch("vibe.cli._write_status") as write_status,
    ):
        cli.cmd_vibe()

    stop_service.assert_not_called()
    stop_ui.assert_not_called()
    write_status.assert_called_once_with("setup", "missing platform credentials")
    output = capsys.readouterr().out
    assert "Run: vibe remote" in output
    assert "SSH port forwarding" not in output
    _assert_start_receipt(output)


def test_cmd_vibe_marks_starting_when_non_slack_platform_is_configured(capsys) -> None:
    config = _config_with_setup_state(ready=True)

    with (
        patch("vibe.cli.paths.ensure_data_dirs"),
        patch("vibe.cli._ensure_config", return_value=config),
        patch("vibe.cli.runtime.stop_service") as stop_service,
        patch("vibe.cli.runtime.stop_ui") as stop_ui,
        patch("vibe.cli.runtime.start_service", side_effect=partial(_start_process, 101)),
        patch("vibe.cli.runtime.start_ui", side_effect=partial(_start_process, 202)),
        patch("vibe.cli.runtime.service_pid_recorded", return_value=True),
        patch("vibe.cli.runtime.write_status"),
        patch("vibe.cli._write_status") as write_status,
    ):
        cli.cmd_vibe()

    stop_service.assert_not_called()
    stop_ui.assert_not_called()
    write_status.assert_called_once_with("starting")
    _assert_start_receipt(capsys.readouterr().out)
