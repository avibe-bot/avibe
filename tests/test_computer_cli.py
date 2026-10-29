from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from jsonschema import Draft7Validator
import pytest

from core import internal_server, session_turns
from core.computer_cli import (
    _COMPUTER_ERROR_I18N_KEYS,
    COMPUTER_CLI_TOOLS,
    build_driver_arguments,
    format_human_result,
    prepare_arguments_for_output,
    tool_for_command,
    write_screenshot,
)
from modules.im import MessageContext
from vibe import cli
from vibe.internal_client import InternalServerTimeout, computer_use_sync
from vibe.i18n import t as i18n_t


def _structured(payload: dict) -> dict:
    return {
        "isError": False,
        "structuredContent": payload,
        "content": [
            {
                "type": "text",
                "text": "Structured content:\n" + json.dumps(payload),
            }
        ],
    }


def _translate(key: str, **kwargs) -> str:
    return i18n_t(key, "en", **kwargs)


def _bind_socket(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    path.chmod(0o600)
    return listener


def test_parser_covers_the_allowlisted_driver_operations_without_session_id() -> None:
    commands = [
        (["computer", "run", "./script.mjs"], ("run",)),
        (["computer", "js", "--code", "1"], ("js",)),
        (["computer", "js-reset"], ("js-reset",)),
        (["computer", "observe", "apps"], ("observe", "apps")),
        (["computer", "observe", "windows"], ("observe", "windows")),
        (["computer", "observe", "window"], ("observe", "window")),
        (["computer", "zoom"], ("zoom",)),
        (["computer", "activate"], ("activate",)),
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
        (["computer", "force-quit"], ("force-quit",)),
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
        if path not in {("run",), ("js",), ("js-reset",)}:
            assert tool_for_command(path)

    with pytest.raises(SystemExit):
        parser.parse_args(["computer", "click", "--session-id", "manual"])
    with pytest.raises(SystemExit):
        parser.parse_args(["computer", "quit"])


def test_packaged_skill_examples_parse_and_match_the_pinned_driver_schema() -> None:
    skill = Path("skills/computer-use/SKILL.md").read_text(encoding="utf-8")
    commands = [
        line.strip()
        for line in skill.splitlines()
        if line.strip().startswith("vibe computer ")
    ]
    commands.extend(re.findall(r"`(vibe computer [^`]+)`", skill))
    commands = list(dict.fromkeys(commands))
    snapshot = json.loads(
        Path("desktop/cua-driver/tools-v0.31.0.json").read_text(encoding="utf-8")
    )
    schemas = {
        tool["name"]: tool["inputSchema"]
        for tool in snapshot["tools"]
        if tool["name"] in COMPUTER_CLI_TOOLS.values()
    }
    parser = cli.build_parser()
    covered_tools: set[str] = set()

    for command in commands:
        argv = shlex.split(command)
        assert argv[:2] == ["vibe", "computer"]
        args = parser.parse_args(argv[1:])
        if args.computer_command == "run":
            assert args.file.endswith(".mjs")
            continue
        if args.computer_command == "js":
            assert args.code.strip() and 1 <= args.timeout_ms <= 300000
            continue
        if args.computer_command == "js-reset":
            continue
        if args.computer_command == "observe":
            path = ("observe", args.computer_observe_command)
        elif args.computer_command == "clipboard":
            path = ("clipboard", args.computer_clipboard_command)
        else:
            path = (args.computer_command,)
        tool = tool_for_command(path)
        arguments = build_driver_arguments(args.args_json, args.arg)
        arguments = prepare_arguments_for_output(tool, arguments, args.output)
        errors = sorted(
            Draft7Validator(schemas[tool]).iter_errors(arguments),
            key=lambda error: tuple(error.path),
        )
        assert not errors, f"{command}: {[error.message for error in errors]}"
        covered_tools.add(tool)

    assert covered_tools == set(COMPUTER_CLI_TOOLS.values())
    assert re.findall(
        r"vibe computer clipboard read[^\n]*",
        skill,
    ) == ["vibe computer clipboard read --arg include_text=true"]

    documented_flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", skill))
    assert documented_flags == {
        "--arg",
        "--args-json",
        "--code",
        "--json",
        "--output",
        "--timeout-ms",
    }
    documented_js = {
        command.split()[2] for command in commands if command.split()[2].startswith("js")
    }
    assert documented_js == {"js", "js-reset"}
    parser.parse_args(
        [
            "computer",
            "observe",
            "windows",
            "--args-json",
            '{"on_screen_only":true}',
            "--json",
        ]
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["computer", "observe", "apps", "--session-id", "manual"])


def test_argument_contract_rejects_manual_session_and_maps_screenshot_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        build_driver_arguments(None, ["session=manual"])
    assert exc_info.value.key == "computer.cli.error.sessionArgumentForbidden"

    output = tmp_path / "zoom.png"
    arguments = prepare_arguments_for_output("zoom", {}, str(output))
    assert arguments == {}
    assert prepare_arguments_for_output(
        "get_window_state",
        {"pid": 7, "window_id": 9},
        str(output),
    )["screenshot_out_file"] == str(output.resolve())


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
        translate=_translate,
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
    assert payload["result"]["structuredContent"]["apps"][0]["name"] == "Fixture"
    assert payload["result"]["content"][0]["type"] == "text"


def test_cmd_computer_run_passes_string_arguments_and_prints_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
    captured: dict = {}

    def fake_run_computer_script(**kwargs):
        captured.update(kwargs)
        return {"mailbox": "Inbox", "count": "01"}

    monkeypatch.setattr(
        "vibe.computer_script_runner.run_computer_script",
        fake_run_computer_script,
    )
    args = cli.build_parser().parse_args(
        [
            "computer",
            "run",
            "./inspect.mjs",
            "--arg",
            "mailbox=Inbox",
            "--arg",
            "count=01",
        ]
    )
    assert cli.cmd_computer(args) == 0
    assert captured == {
        "caller_session_id": "ses-cli",
        "script_file": "./inspect.mjs",
        "arg_values": ["mailbox=Inbox", "count=01"],
    }
    assert json.loads(capsys.readouterr().out) == {
        "count": "01",
        "mailbox": "Inbox",
    }


def test_cmd_computer_run_reports_missing_private_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe.computer_script_runner import ComputerScriptRunnerError

    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")

    def unavailable(**_kwargs):
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptNodeUnavailable",
            code="computer_script_unavailable",
        )

    monkeypatch.setattr(
        "vibe.computer_script_runner.run_computer_script",
        unavailable,
    )
    args = cli.build_parser().parse_args(["computer", "run", "./inspect.mjs"])
    with pytest.raises(
        cli.TaskCliError,
        match="Node runtime shipped with the Avibe desktop app",
    ) as exc_info:
        cli.cmd_computer(args)
    assert exc_info.value.code == "computer_script_unavailable"


def test_cmd_computer_run_reports_sdk_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibe.computer_script_runner import ComputerScriptRunnerError

    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")

    def invalid_selector(**_kwargs):
        raise ComputerScriptRunnerError(
            "computer.cli.error.scriptFailed",
            code="invalid_selector",
            params={
                "step": "computer.app",
                "error": "Application selector contains unknown field 'text'.",
            },
            structured_error={
                "code": "invalid_selector",
                "params": {"field": "text"},
            },
        )

    monkeypatch.setattr(
        "vibe.computer_script_runner.run_computer_script",
        invalid_selector,
    )
    args = cli.build_parser().parse_args(["computer", "run", "./inspect.mjs"])

    with pytest.raises(cli.TaskCliError) as exc_info:
        cli.cmd_computer(args)

    assert str(exc_info.value).startswith("Computer Use script failed at computer.app")
    assert "Application selector" not in str(exc_info.value)


def test_computer_cli_error_translations_exist_in_all_supported_languages() -> None:
    keys = {
        *_COMPUTER_ERROR_I18N_KEYS.values(),
        "computer.cli.error.screenshotMissing",
        "computer.cli.error.scriptArgNameValue",
        "computer.cli.error.scriptArgDuplicate",
        "computer.cli.error.scriptArgFile",
        "computer.cli.error.scriptFile",
        "computer.cli.error.scriptFileType",
        "computer.cli.error.scriptNodeUnavailable",
        "computer.cli.error.scriptSdkUnavailable",
        "computer.cli.error.scriptOpenRejected",
        "computer.cli.error.scriptRuntime",
        "computer.cli.error.scriptRuntimeExit",
        "computer.cli.error.scriptRuntimeInvalid",
        "computer.cli.error.scriptTimedOut",
        "computer.cli.error.scriptFailed",
        "computer.cli.error.jsFailed",
        "computer.cli.error.jsCodeFile",
        "computer.cli.error.jsTimeoutRange",
    }
    for catalog_path in sorted(Path("vibe/i18n").glob("*.json")):
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        for key in keys:
            value: object = catalog
            for part in key.split("."):
                assert isinstance(value, dict) and part in value, (
                    f"{catalog_path.stem} is missing {key}"
                )
                value = value[part]
            assert isinstance(value, str) and value.strip(), (
                f"{catalog_path.stem} has an empty translation for {key}"
            )


def test_cmd_computer_reports_missing_screenshot_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = (
        "No screenshot is available for this result. The window may be hidden or "
        "minimized, or capture may be unavailable."
    )
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")

    with patch(
        "vibe.internal_client.computer_use_sync",
        return_value={
            "status_code": 200,
            "body": {
                "ok": True,
                "tool": "get_window_state",
                "result": _structured({"pid": 7, "window_id": 9}),
            },
        },
    ):
        args = cli.build_parser().parse_args(
            [
                "computer",
                "observe",
                "window",
                "--output",
                str(tmp_path / "window.png"),
            ]
        )
        with pytest.raises(cli.TaskCliError) as exc_info:
            cli.cmd_computer(args)

    assert str(exc_info.value) == expected
    assert "computer.cli.error.screenshotMissing" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("argv", "expected_tool", "expected_arguments"),
    [
        (
            ["observe", "window", "--arg", "pid=7", "--arg", "window_id=9"],
            "get_window_state",
            {"pid": 7, "window_id": 9},
        ),
        (
            [
                "zoom",
                "--arg",
                "pid=7",
                "--arg",
                "window_id=9",
                "--arg",
                "x1=0",
                "--arg",
                "y1=0",
                "--arg",
                "x2=100",
                "--arg",
                "y2=100",
            ],
            "zoom",
            {
                "pid": 7,
                "window_id": 9,
                "x1": 0,
                "y1": 0,
                "x2": 100,
                "y2": 100,
            },
        ),
    ],
)
def test_cmd_computer_reports_driver_error_before_writing_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    argv: list[str],
    expected_tool: str,
    expected_arguments: dict,
) -> None:
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
    output_path = tmp_path / f"{expected_tool}.png"

    def fake_computer_use_sync(**kwargs):
        assert kwargs["tool"] == expected_tool
        if expected_tool == "get_window_state":
            expected_arguments["screenshot_out_file"] = str(output_path.resolve())
        assert kwargs["arguments"] == expected_arguments
        return {
            "status_code": 200,
            "body": {
                "ok": False,
                "tool": expected_tool,
                "result": {
                    "isError": True,
                    "structuredContent": {
                        "code": "target_busy",
                        "params": {},
                    },
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Structured content:\n"
                                + json.dumps(
                                    {
                                        "code": "target_busy",
                                        "params": {},
                                    }
                                )
                            ),
                        }
                    ],
                },
            },
        }

    with patch("vibe.internal_client.computer_use_sync", fake_computer_use_sync):
        args = cli.build_parser().parse_args(
            ["computer", *argv, "--output", str(output_path)]
        )
        assert cli.cmd_computer(args) == 1

    output = capsys.readouterr().out
    assert "The requested Computer Use target is busy" in output
    assert "desktop is held" not in output
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("status", "reason", "zh_status", "zh_reason"),
    [
        ("off", "toggle_off", "未开启", "原生电脑操作开关已关闭"),
        (
            "needs_permission",
            "screen_recording",
            "需要系统权限",
            "缺少屏幕录制权限",
        ),
        ("starting", None, "桌面驱动正在启动", "未提供原因"),
    ],
)
def test_human_unavailable_error_localizes_structured_status_and_reason(
    status: str,
    reason: str | None,
    zh_status: str,
    zh_reason: str,
) -> None:
    result = {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "code": "computer_use_unavailable",
                        "params": {"status": status, "reason": reason},
                    }
                ),
            }
        ],
    }

    output = format_human_result(
        "list_apps",
        result,
        translate=lambda key, **kwargs: i18n_t(key, "zh", **kwargs),
    )

    assert f"状态={status}：{zh_status}" in output
    assert zh_reason in output
    assert "Computer use is not ready" not in output
    assert "permission is required" not in output


def test_json_unavailable_error_keeps_code_and_params(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
    body = {
        "ok": False,
        "tool": "list_apps",
        "result": {
            "isError": True,
            "structuredContent": {
                "code": "computer_use_unavailable",
                "params": {
                    "status": "needs_permission",
                    "reason": "screen_recording",
                },
            },
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "code": "computer_use_unavailable",
                            "params": {
                                "status": "needs_permission",
                                "reason": "screen_recording",
                            },
                        }
                    ),
                }
            ],
        },
    }

    with patch(
        "vibe.internal_client.computer_use_sync",
        lambda **_kwargs: {"status_code": 200, "body": body},
    ):
        args = cli.build_parser().parse_args(
            ["computer", "observe", "apps", "--json"]
        )
        assert cli.cmd_computer(args) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["structuredContent"]["params"]["reason"] == (
        "screen_recording"
    )
    assert payload == body


def test_cmd_computer_reports_controller_error_in_english(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")

    def fake_computer_use_sync(**kwargs):
        del kwargs
        return {
            "status_code": 409,
            "body": {
                "ok": False,
                "error": "session_archived",
                "params": {},
            },
        }

    with patch("vibe.internal_client.computer_use_sync", fake_computer_use_sync):
        args = cli.build_parser().parse_args(["computer", "observe", "apps"])
        assert cli.cmd_computer(args) == 1

    output = capsys.readouterr().out
    assert "Computer Use is unavailable because this Avibe Session is archived or being archived." in output


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


def test_internal_client_treats_post_connect_transport_failure_as_ambiguous() -> None:
    with tempfile.TemporaryDirectory(prefix="avibe-uds-", dir="/tmp") as directory:
        socket_path = Path(directory) / "dispatch.sock"
        listener = _bind_socket(socket_path)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError(
                "connection closed after the request",
                request=request,
            )

        try:
            with patch(
                "vibe.internal_client.httpx.HTTPTransport",
                return_value=httpx.MockTransport(handler),
            ):
                with pytest.raises(
                    InternalServerTimeout,
                    match="may have completed; observe again before retrying",
                ):
                    computer_use_sync(
                        caller_session_id="ses-cli",
                        tool="click",
                        arguments={"pid": 7, "window_id": 9},
                        socket_path=socket_path,
                    )
        finally:
            listener.close()


def test_internal_route_binds_computer_use_to_the_active_session() -> None:
    async def exercise() -> tuple[dict, dict, dict, dict, list[tuple[str, dict]]]:
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
                turn_capability = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "caller_turn_id": "stale-turn",
                        "tool": "list_windows",
                        "arguments": {},
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
            return (
                success.json(),
                forbidden.json(),
                turn_capability.json(),
                mismatch.json(),
                calls,
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    success, forbidden, turn_capability, mismatch, calls = asyncio.run(exercise())

    assert success["ok"] is True
    assert success["session_id"] == "ses-cli"
    assert success.get("turn_id") is None
    assert calls == [
        ("list_windows", {"on_screen_only": True, "session": "ses-cli"}),
    ]
    assert forbidden["error"] == "session_argument_forbidden"
    assert forbidden["params"] == {}
    assert "message" not in forbidden
    assert turn_capability["error"] == "invalid_computer_use_request"
    assert turn_capability["params"] == {}
    assert "message" not in turn_capability
    assert mismatch["error"] == "active_turn_required"
    assert mismatch["params"] == {}
    assert "message" not in mismatch


def test_internal_route_requires_a_server_side_active_turn() -> None:
    async def exercise() -> tuple[dict, list[tuple[str, dict]]]:
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
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/internal/computer-use",
                json={
                    "caller_session_id": "ses-cli",
                    "tool": "list_windows",
                    "arguments": {},
                },
            )
        return response.json(), calls

    response, calls = asyncio.run(exercise())
    assert response["error"] == "active_turn_required"
    assert response["params"] == {}
    assert "message" not in response
    assert calls == []


def test_archive_lifecycle_fences_computer_use_admission() -> None:
    async def exercise() -> tuple[dict, dict, list[tuple[str, dict]]]:
        calls: list[tuple[str, dict]] = []
        archive_started = asyncio.Event()
        release_archive = asyncio.Event()

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
        turn_task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="user",
            channel_id="channel",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-cli", "turn_token": "turn-1"},
        )
        controller.session_turns.in_flight["ses-cli"] = session_turns.Turn(
            task=turn_task,
            context=context,
            logical_turn_id="turn-1",
        )

        async def archive_operation() -> None:
            archive_started.set()
            await release_archive.wait()

        archive_task = asyncio.create_task(
            controller.session_turns.run_session_lifecycle(
                "ses-cli",
                archive_operation,
            )
        )
        try:
            await archive_started.wait()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                during_archive = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
                release_archive.set()
                await archive_task
                after_archive = await client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
            return during_archive.json(), after_archive.json(), calls
        finally:
            release_archive.set()
            if not archive_task.done():
                await archive_task
            turn_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn_task

    during_archive, after_archive, calls = asyncio.run(exercise())

    assert during_archive["error"] == "session_archived"
    assert after_archive["error"] == "session_archived"
    assert calls == []


def test_turn_end_does_not_cancel_session_keyed_computer_use_request() -> None:
    async def exercise() -> tuple[dict, list[tuple[str, dict]]]:
        calls: list[tuple[str, dict]] = []
        started = asyncio.Event()
        release = asyncio.Event()

        class ComputerServer:
            async def call_tool(self, name: str, arguments: dict) -> dict:
                calls.append((name, arguments))
                started.set()
                await release.wait()
                return _structured({"windows": []})

        controller = SimpleNamespace(
            computer_use_server=ComputerServer(),
            controller_id="controller-test",
            _session_id_from_context=lambda context: (
                (getattr(context, "platform_specific", None) or {}).get("agent_session_id")
            ),
        )
        app = internal_server.create_app(controller)
        turn_task = asyncio.create_task(asyncio.sleep(60))
        context = MessageContext(
            user_id="user",
            channel_id="channel",
            platform="avibe",
            platform_specific={"agent_session_id": "ses-cli", "turn_token": "turn-1"},
        )
        controller.session_turns.in_flight["ses-cli"] = session_turns.Turn(
            task=turn_task,
            context=context,
            logical_turn_id="turn-1",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(
                    "/internal/computer-use",
                    json={
                        "caller_session_id": "ses-cli",
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
            )
            await started.wait()
            controller.session_turns.in_flight.pop("ses-cli")
            release.set()
            response = await request_task
        turn_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn_task
        return response.json(), calls

    response, calls = asyncio.run(exercise())
    assert response["ok"] is True
    assert calls == [("list_windows", {"session": "ses-cli"})]
