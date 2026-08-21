from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from vibe import cli


def _config_with_setup_state(*, ready: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ui=SimpleNamespace(setup_host="127.0.0.1", setup_port=5123, open_browser=False),
        slack=SimpleNamespace(bot_token=""),
        has_configured_platform_credentials=lambda: ready,
    )


def test_cmd_vibe_marks_setup_when_no_enabled_platform_has_credentials(capsys) -> None:
    config = _config_with_setup_state(ready=False)

    with (
        patch("vibe.cli.paths.ensure_data_dirs"),
        patch("vibe.cli._ensure_config", return_value=config),
        patch("vibe.cli.runtime.stop_service") as stop_service,
        patch("vibe.cli.runtime.stop_ui") as stop_ui,
        patch("vibe.cli.runtime.start_service", return_value=101),
        patch("vibe.cli.runtime.start_ui", return_value=202),
        patch("vibe.cli.runtime.wait_for_service_ready", return_value=101),
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


def test_cmd_vibe_marks_starting_when_non_slack_platform_is_configured() -> None:
    config = _config_with_setup_state(ready=True)

    with (
        patch("vibe.cli.paths.ensure_data_dirs"),
        patch("vibe.cli._ensure_config", return_value=config),
        patch("vibe.cli.runtime.stop_service") as stop_service,
        patch("vibe.cli.runtime.stop_ui") as stop_ui,
        patch("vibe.cli.runtime.start_service", return_value=101),
        patch("vibe.cli.runtime.start_ui", return_value=202),
        patch("vibe.cli.runtime.wait_for_service_ready", return_value=101),
        patch("vibe.cli.runtime.write_status"),
        patch("vibe.cli._write_status") as write_status,
    ):
        cli.cmd_vibe()

    stop_service.assert_not_called()
    stop_ui.assert_not_called()
    write_status.assert_called_once_with("starting")
