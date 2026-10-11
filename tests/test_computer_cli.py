from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from core import internal_server, session_turns
from core.computer_cli import (
    build_driver_arguments,
    format_human_result,
    prepare_arguments_for_output,
    tool_for_command,
    write_screenshot,
)
from modules.im import MessageContext
from vibe import cli
from vibe.internal_client import computer_use_sync


def _structured(payload: dict) -> dict:
    return {
        "isError": False,
        "content": [
            {
                "type": "text",
                "text": "Structured content:\n" + json.dumps(payload),
            }
        ],
    }


def _bind_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    path.chmod(0o600)
    return listener


def test_parser_covers_the_allowlisted_driver_operations_without_session_id() -> None:
    commands = [
        (["computer", "observe", "apps"], ("observe", "apps")),
        (["computer", "observe", "windows"], ("observe", "windows")),
        (["computer", "observe", "window"], ("observe", "window")),
        (["computer", "zoom"], ("zoom",)),
        (["computer", "click"], ("click",)),
        (["computer", "type"], ("type",)),
        (["computer", "key"], ("key",)),
        (["computer", "hotkey"], ("hotkey",)),
        (["computer", "scroll"], ("scroll",)),
        (["computer", "drag"], ("drag",)),
        (["computer", "set-value"], ("set-value",)),
        (["computer", "clipboard", "read"], ("clipboard", "read")),
        (["computer", "clipboard", "write"], ("clipboard", "write")),
        (["computer", "launch"], ("launch",)),
        (["computer", "quit"], ("quit",)),
    ]

    parser = cli.build_parser()
    for argv, path in commands:
        args = parser.parse_args(argv)
        if path[0] == "observe":
            command_path = ("observe", args.computer_observe_command)
        elif path[0] == "clipboard":
            command_path = ("clipboard", args.computer_clipboard_command)
        else:
            command_path = (args.computer_command,)
        assert command_path == path
        assert tool_for_command(path)

    with pytest.raises(SystemExit):
        parser.parse_args(["computer", "click", "--session-id", "manual"])


def test_argument_contract_rejects_manual_session_and_maps_screenshot_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session is bound"):
        build_driver_arguments(None, ["session=manual"])

    output = tmp_path / "zoom.png"
    arguments = prepare_arguments_for_output("zoom", {}, str(output))
    assert arguments == {}
    assert prepare_arguments_for_output(
        "get_window_state",
        {"pid": 7, "window_id": 9},
        str(output),
    )["screenshot_out_file"] == str(output)


def test_human_observation_output_surfaces_accessibility_and_tokens() -> None:
    output = format_human_result(
        "get_window_state",
        _structured(
            {
                "elements_complete": False,
                "degraded_reason": "timeout",
                "elements": [
                    {"element_token": "el_1", "role": "button", "label": "Continue"}
                ],
            }
        ),
    )

    assert "Accessibility: degraded (timeout)" in output
    assert "Element tokens:" in output
    assert "el_1 [button] Continue" in output


def test_screenshot_outputs_write_both_file_and_embedded_images(tmp_path: Path) -> None:
    target = tmp_path / "window.png"
    result = _structured({"screenshot_file_path": str(target)})
    assert write_screenshot("get_window_state", result, str(target)) == str(target.resolve())

    zoom_target = tmp_path / "zoom.png"
    zoom_result = {
        "isError": False,
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(b"png-bytes").decode("ascii"),
                "mimeType": "image/png",
            }
        ],
    }
    assert write_screenshot("zoom", zoom_result, str(zoom_target)) == str(zoom_target.resolve())
    assert zoom_target.read_bytes() == b"png-bytes"


def test_cmd_computer_binds_the_caller_session_and_prints_structured_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
    captured: dict = {}

    def fake_computer_use_sync(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "tool": "list_apps",
                "session_id": "ses-cli",
                "turn_id": "turn-1",
                "result": _structured({"apps": [{"pid": 7, "name": "Fixture"}]}),
            },
        }

    with patch("vibe.internal_client.computer_use_sync", fake_computer_use_sync):
        args = cli.build_parser().parse_args(["computer", "observe", "apps", "--json"])
        assert cli.cmd_computer(args) == 0

    assert captured == {
        "caller_session_id": "ses-cli",
        "tool": "list_apps",
        "arguments": {},
    }
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "ses-cli"
    assert payload["result"]["content"][0]["type"] == "text"


def test_cmd_computer_requires_an_injected_caller_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVIBE_SESSION_ID", raising=False)
    args = cli.build_parser().parse_args(["computer", "observe", "apps"])

    with pytest.raises(cli.TaskCliError, match="requires an Avibe Agent caller Session"):
        cli.cmd_computer(args)


def test_internal_client_forwards_the_caller_identity_and_never_adds_a_session_argument(
    tmp_path: Path,
) -> None:
    del tmp_path
    with tempfile.TemporaryDirectory(prefix="avibe-uds-", dir="/tmp") as directory:
        socket_path = Path(directory) / "dispatch.sock"
        listener = _bind_socket(socket_path)
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        try:
            with patch(
                "vibe.internal_client.httpx.HTTPTransport",
                return_value=httpx.MockTransport(handler),
            ):
                result = computer_use_sync(
                    caller_session_id="ses-cli",
                    tool="click",
                    arguments={"pid": 7, "window_id": 9},
                    socket_path=socket_path,
                )
        finally:
            listener.close()

    assert result["body"]["ok"] is True
    assert captured == {
        "caller_session_id": "ses-cli",
        "tool": "click",
        "arguments": {"pid": 7, "window_id": 9},
    }


def test_internal_route_binds_computer_use_to_the_active_turn() -> None:
    async def exercise() -> tuple[dict, dict, dict]:
        calls: list[tuple[str, dict]] = []

        class ComputerServer:
            async def call_tool(self, name: str, arguments: dict) -> dict:
                calls.append((name, arguments))
                return _structured({"windows": []})

        controller = SimpleNamespace(
            computer_use_server=ComputerServer(),
            controller_id="controller-test",
            _session_id_from_context=lambda context: (
                (getattr(context, "platform_specific", None) or {}).get("agent_session_id")
            ),
        )
        app = internal_server.create_app(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="user",
            channel_id="channel",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-cli", "turn_token": "turn-1"},
        )
        controller.session_turns.in_flight["ses-cli"] = session_turns.Turn(task=task, context=context)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                success = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "tool": "list_windows",
                        "arguments": {"on_screen_only": True},
                    },
                )
                forbidden = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "tool": "list_windows",
                        "arguments": {"session": "manual"},
                    },
                )
                mismatch = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "other-session",
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
            return success.json(), forbidden.json(), mismatch.json()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    success, forbidden, mismatch = asyncio.run(exercise())

    assert success["ok"] is True
    assert success["session_id"] == "ses-cli"
    assert success["turn_id"] == "turn-1"
    assert forbidden["error"] == "session_argument_forbidden"
    assert mismatch["error"] == "active_turn_required"
