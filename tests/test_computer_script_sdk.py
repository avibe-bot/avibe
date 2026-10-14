from __future__ import annotations

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Iterator
from unittest.mock import patch

import httpx
import pytest

from vibe import cli, computer_script_runner
from vibe.computer_script_runner import (
    ComputerScriptRunnerError,
    parse_script_arguments,
)
from vibe.internal_client import (
    computer_script_close_sync,
    computer_script_endpoint_payload,
    computer_script_open_sync,
    resolve_computer_script_endpoint,
)


NODE = shutil.which("node")
SDK_DIRECTORY = Path("vibe/computer_sdk").resolve()


def _result(payload: dict, *, is_error: bool = False, image: bool = False) -> dict:
    content: list[dict] = [
        {"type": "text", "text": "Structured content:\n" + json.dumps(payload)}
    ]
    if image:
        content.append(
            {
                "type": "image",
                "data": base64.b64encode(b"png-fixture").decode("ascii"),
                "mimeType": "image/png",
            }
        )
    return {
        "ok": not is_error,
        "result": {
            "isError": is_error,
            "structuredContent": payload,
            "content": content,
        },
    }


@contextmanager
def _fake_controller(
    *,
    click_error: str | None = None,
    duplicate_within: bool = False,
    dynamic_elements_after_action: bool = False,
    response_delay_by_tool: dict[str, float] | None = None,
    apps: list[dict] | None = None,
    app_responses: list[list[dict]] | None = None,
    windows: list[dict] | None = None,
    launch_pid: int | None = 41,
) -> Iterator[tuple[ThreadingHTTPServer, list[dict]]]:
    requests: list[dict] = []
    observations = 0
    clicks = 0
    app_lookups = 0
    connection_ids: set[int] = set()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            nonlocal observations, clicks, app_lookups
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            connection_ids.add(id(self.connection))
            requests.append(
                {
                    "path": self.path,
                    "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if self.path == "/internal/computer-script/close":
                body = {"ok": True, "closed": True}
            else:
                tool = payload["tool"]
                delay = (response_delay_by_tool or {}).get(tool, 0)
                if delay:
                    time.sleep(delay)
                if tool == "list_apps":
                    if app_responses is not None:
                        app_records = app_responses[
                            min(app_lookups, len(app_responses) - 1)
                        ]
                        app_lookups += 1
                    else:
                        app_records = apps
                    body = _result(
                        {
                            "apps": app_records
                            if app_records is not None
                            else [
                                {
                                    "pid": 41,
                                    "name": "Mail",
                                    "bundle_id": "com.apple.mail",
                                    "running": True,
                                    "active": False,
                                }
                            ]
                        }
                    )
                elif tool == "list_windows":
                    body = _result(
                        {
                            "windows": windows
                            or [
                                {
                                    "pid": 41,
                                    "window_id": 9,
                                    "app_name": "Mail",
                                    "title": "New Message",
                                    "bounds": {
                                        "x": 0,
                                        "y": 0,
                                        "width": 800,
                                        "height": 600,
                                    },
                                    "is_on_screen": True,
                                    "z_index": 1,
                                }
                            ],
                        }
                    )
                elif tool == "get_window_state":
                    observations += 1
                    elements = [
                        {
                            "element_index": 1,
                            "element_token": f"group-{observations}",
                            "role": "AXGroup",
                            "label": "Composer",
                            "value": None,
                            "parent_index": None,
                            "depth": 0,
                        },
                        {
                            "element_index": 2,
                            "element_token": f"send-{observations}",
                            "role": "AXButton",
                            "label": "Send",
                            "value": None,
                            "parent_index": 1,
                            "depth": 1,
                        },
                        {
                            "element_index": 3,
                            "element_token": f"subject-{observations}",
                            "role": "AXTextField",
                            "label": "Subject",
                            "value": "Quarterly report",
                            "parent_index": 1,
                            "depth": 1,
                        },
                        {
                            "element_index": 4,
                            "element_token": f"send-alt-{observations}",
                            "role": "AXButton",
                            "label": "Send",
                            "value": None,
                            "parent_index": 1,
                            "depth": 1,
                        },
                    ]
                    if dynamic_elements_after_action:
                        elements = [
                            {
                                "element_index": 1,
                                "element_token": f"group-{observations}",
                                "role": "AXGroup",
                                "label": "Items",
                                "value": None,
                                "parent_index": None,
                                "depth": 0,
                            },
                            {
                                "element_index": 2,
                                "element_token": f"add-{observations}",
                                "role": "AXButton",
                                "label": "Add",
                                "value": None,
                                "parent_index": 1,
                                "depth": 1,
                            },
                            {
                                "element_index": 3,
                                "element_token": f"delete-a-{observations}",
                                "role": "AXButton",
                                "label": "Delete",
                                "value": None,
                                "parent_index": 1,
                                "depth": 1,
                            },
                        ]
                        if clicks >= 2:
                            elements.append(
                                {
                                    "element_index": 4,
                                    "element_token": f"delete-b-{observations}",
                                    "role": "AXButton",
                                    "label": "Delete",
                                    "value": None,
                                    "parent_index": 1,
                                    "depth": 1,
                                }
                            )
                    if duplicate_within:
                        elements = [
                            {
                                "element_index": 1,
                                "element_token": f"group-a-{observations}",
                                "role": "AXGroup",
                                "label": "Composer",
                                "value": None,
                                "parent_index": None,
                                "depth": 0,
                            },
                            {
                                "element_index": 2,
                                "element_token": f"group-b-{observations}",
                                "role": "AXGroup",
                                "label": "Composer",
                                "value": None,
                                "parent_index": None,
                                "depth": 0,
                            },
                            {
                                "element_index": 3,
                                "element_token": f"send-{observations}",
                                "role": "AXButton",
                                "label": "Send",
                                "value": None,
                                "parent_index": 1,
                                "depth": 1,
                            },
                        ]
                    if (
                        payload["arguments"].get("include_accessibility_tree")
                        is False
                    ):
                        elements = None
                    body = _result(
                        {
                            "pid": 41,
                            "window_id": 9,
                            **({"elements": elements} if elements is not None else {}),
                        },
                        image=True,
                    )
                elif tool == "click":
                    clicks += 1
                    if click_error is not None:
                        body = _result(
                            {"code": click_error, "params": {}},
                            is_error=True,
                        )
                    elif clicks == 1:
                        body = _result(
                            {"code": "observe_first", "params": {}},
                            is_error=True,
                        )
                    else:
                        body = _result(
                            {
                                "effect": "confirmed",
                                "route": "accessibility",
                            }
                        )
                elif tool == "double_click":
                    body = _result(
                        {
                            "effect": "confirmed",
                            "route": "accessibility",
                        }
                    )
                elif tool == "kill_app":
                    body = _result(
                        {
                            "effect": "confirmed",
                            "route": "system_api",
                        }
                    )
                elif tool == "launch_app":
                    launch_payload = {
                        "effect": "confirmed",
                        "route": "system_api",
                    }
                    if launch_pid is not None:
                        launch_payload["pid"] = launch_pid
                    body = _result(launch_payload)
                else:
                    body = _result(
                        {"code": "unknown_tool", "params": {"tool": tool}},
                        is_error=True,
                    )
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("X-Avibe-Control-Instance", "fixture-instance")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.connection_ids = connection_ids  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_node_script(
    tmp_path: Path,
    source: str,
    *,
    click_error: str | None = None,
    duplicate_within: bool = False,
    dynamic_elements_after_action: bool = False,
    response_delay_by_tool: dict[str, float] | None = None,
    apps: list[dict] | None = None,
    app_responses: list[list[dict]] | None = None,
    windows: list[dict] | None = None,
    launch_pid: int | None = 41,
) -> tuple[dict, ThreadingHTTPServer, list[dict]]:
    if NODE is None:
        pytest.skip("Node.js is unavailable")
    script = tmp_path / "script.mjs"
    script.write_text(source, encoding="utf-8")
    shadow = tmp_path / "node_modules" / "@avibe" / "computer"
    shadow.mkdir(parents=True)
    (shadow / "package.json").write_text(
        '{"name":"@avibe/computer","type":"module","exports":"./index.mjs"}',
        encoding="utf-8",
    )
    (shadow / "index.mjs").write_text(
        'throw new Error("project-local shadow loaded");',
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    with _fake_controller(
        click_error=click_error,
        duplicate_within=duplicate_within,
        dynamic_elements_after_action=dynamic_elements_after_action,
        response_delay_by_tool=response_delay_by_tool,
        apps=apps,
        app_responses=app_responses,
        windows=windows,
        launch_pid=launch_pid,
    ) as (server, requests):
        request = {
            "script_url": script.resolve().as_uri(),
            "sdk_url": (SDK_DIRECTORY / "index.mjs").as_uri(),
            "arguments": {"subject": "Quarterly report"},
            "endpoint": {
                "transport": "tcp",
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "headers": {"Authorization": "Bearer fixture-token"},
                "instance_id": "fixture-instance",
                "instance_header": "X-Avibe-Control-Instance",
            },
            "connection_token": "script-token",
            "artifact_directory": str(artifacts),
            "runtime_state_path": str(artifacts / "runtime-state.json"),
        }
        started = time.monotonic()
        completed = computer_script_runner._run_node_process(
            Path(NODE),
            SDK_DIRECTORY / "runner.mjs",
            request,
            10,
        )
        server.subprocess_elapsed = time.monotonic() - started  # type: ignore[attr-defined]
        server.completed = completed  # type: ignore[attr-defined]
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout), server, requests


def _run_python_deadline_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    *,
    response_delay_by_tool: dict[str, float] | None = None,
    timeout_seconds: float = 0.25,
) -> tuple[ComputerScriptRunnerError, list[dict], float]:
    if NODE is None:
        pytest.skip("Node.js is unavailable")
    script = tmp_path / "deadline-script.mjs"
    script.write_text(source, encoding="utf-8")
    endpoint = object()

    with _fake_controller(
        response_delay_by_tool=response_delay_by_tool,
    ) as (server, requests):
        monkeypatch.setattr(
            computer_script_runner,
            "private_desktop_node_bin",
            lambda: Path(NODE),
        )
        monkeypatch.setattr(
            computer_script_runner,
            "resolve_computer_script_endpoint",
            lambda: endpoint,
        )
        monkeypatch.setattr(
            computer_script_runner,
            "computer_script_open_sync",
            lambda **_kwargs: {
                "body": {
                    "ok": True,
                    "connection_token": "script-token",
                }
            },
        )
        monkeypatch.setattr(
            computer_script_runner,
            "computer_script_endpoint_payload",
            lambda value: {
                "transport": "tcp",
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "headers": {"Authorization": "Bearer fixture-token"},
                "instance_id": "fixture-instance",
                "instance_header": "X-Avibe-Control-Instance",
            }
            if value is endpoint
            else {},
        )
        monkeypatch.setattr(
            computer_script_runner,
            "computer_script_close_sync",
            lambda **_kwargs: None,
        )
        started = time.monotonic()
        with pytest.raises(ComputerScriptRunnerError) as exc_info:
            computer_script_runner.run_computer_script(
                caller_session_id="ses-script",
                script_file=str(script),
                arg_values=[],
                timeout_seconds=timeout_seconds,
            )
        elapsed = time.monotonic() - started
    return exc_info.value, requests, elapsed


def test_sdk_injects_module_resolution_and_reobserves_one_stale_token(
    tmp_path: Path,
) -> None:
    payload, server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async ({ subject }) => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.waitFor(
    { role: "AXWindow", labelContains: "New Message" },
    { timeout: 500 },
  );
  const observation = await window.observe();
  const send = window.element({
    role: "button",
    label: "Send",
    within: { role: "group", label: "Composer" },
    index: 0,
  });
  const effect = await send.click();
  await window.expect({ role: "textbox", label: "Subject" }).toHaveValue(
    subject,
    { timeout: 500 },
  );
  return {
    effect: effect.status,
    title: window.title,
    tokenHidden: !Object.hasOwn(observation.elements[0], "element_token"),
  };
};
""",
    )

    assert payload == {
        "ok": True,
        "result": {
            "effect": "confirmed",
            "title": "New Message",
            "tokenHidden": True,
        },
    }
    tools = [
        request["payload"].get("tool")
        for request in requests
        if request["path"].endswith("/request")
    ]
    assert tools.count("click") == 2
    assert tools.count("get_window_state") >= 3
    click_tokens = [
        request["payload"]["arguments"]["element_token"]
        for request in requests
        if request["payload"].get("tool") == "click"
    ]
    assert click_tokens == ["send-2", "send-3"]
    assert requests[-1]["path"] == "/internal/computer-script/close"
    assert all(
        request["authorization"] == "Bearer fixture-token" for request in requests
    )
    assert len(server.connection_ids) == 1  # type: ignore[attr-defined]


def test_sdk_failure_reports_step_and_last_screenshot(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.element({ role: "button", label: "Missing" }).click();
  return {};
};
""",
    )

    assert payload["ok"] is False
    assert payload["error"]["name"] == "NotFound"
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["step"] == "element.click"
    screenshot = Path(payload["error"]["screenshot_path"])
    assert screenshot.read_bytes() == b"png-fixture"


def test_failure_envelope_survives_runner_python_and_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    screenshot_path = tmp_path / "failure.png"
    screenshot_path.write_bytes(b"fixture")
    expected = {
        "name": "FixtureFailure",
        "code": "invalid_selector",
        "message": "Application selector contains unknown field 'text'.",
        "params": {"field": "text"},
        "step": "computer.app",
        "screenshot_path": str(screenshot_path),
        "ambiguous": True,
    }
    assert tuple(expected) == computer_script_runner._failure_envelope_fields()

    payload, _server, _requests = _run_node_script(
        tmp_path,
        f"""
export default async () => {{
  const error = new Error({json.dumps(expected["message"])});
  error.name = {json.dumps(expected["name"])};
  error.code = {json.dumps(expected["code"])};
  error.params = {json.dumps(expected["params"])};
  error.step = {json.dumps(expected["step"])};
  error.screenshotPath = {json.dumps(expected["screenshot_path"])};
  error.ambiguous = true;
  throw error;
}};
""",
    )
    assert payload == {"ok": False, "error": expected}

    script = tmp_path / "python-hop.mjs"
    script.write_text("export default async () => null;", encoding="utf-8")
    endpoint = object()
    monkeypatch.setattr(
        computer_script_runner,
        "private_desktop_node_bin",
        lambda: Path("/private/node"),
    )
    monkeypatch.setattr(
        computer_script_runner,
        "resolve_computer_script_endpoint",
        lambda: endpoint,
    )
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_open_sync",
        lambda **_kwargs: {
            "body": {"ok": True, "connection_token": "script-token"}
        },
    )
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_endpoint_payload",
        lambda value: {"endpoint": value is endpoint},
    )
    monkeypatch.setattr(
        computer_script_runner,
        "_run_node_process",
        lambda *_args: subprocess.CompletedProcess(
            args=["node", "runner.mjs"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_close_sync",
        lambda **_kwargs: None,
    )

    with pytest.raises(ComputerScriptRunnerError) as runner_exc_info:
        computer_script_runner.run_computer_script(
            caller_session_id="ses-script",
            script_file=str(script),
            arg_values=[],
        )
    runner_error = runner_exc_info.value
    assert runner_error.details == expected
    assert runner_error.structured_error == expected

    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-script")
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: "zh")

    def fail_script(**_kwargs):
        raise runner_error

    monkeypatch.setattr(
        computer_script_runner,
        "run_computer_script",
        fail_script,
    )
    args = cli.build_parser().parse_args(["computer", "run", str(script)])
    with pytest.raises(cli.TaskCliError) as cli_exc_info:
        cli.cmd_computer(args)
    cli_error = cli_exc_info.value
    assert cli_error.details == expected
    assert expected["message"] not in str(cli_error)

    cli._print_task_error(cli_error)
    rendered = json.loads(capsys.readouterr().err)
    assert rendered["details"] == expected


def test_sdk_maps_stable_controller_codes_to_typed_errors(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer, DesktopBusy } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  try {
    await window
      .element({ role: "button", label: "Send", index: 0 })
      .click();
  } catch (error) {
    return { typed: error instanceof DesktopBusy, code: error.code };
  }
  throw new Error("desktop_busy was not raised");
};
""",
        click_error="desktop_busy",
    )

    assert payload == {
        "ok": True,
        "result": {"typed": True, "code": "desktop_busy"},
    }


@pytest.mark.parametrize(
    ("controller_code", "error_class"),
    [
        ("element_not_found", "NotFound"),
        ("foreground_forbidden", "ForegroundConsentRequired"),
        ("computer_use_unavailable", "Unavailable"),
    ],
)
def test_sdk_maps_controller_error_families(
    tmp_path: Path,
    controller_code: str,
    error_class: str,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        f"""
import {{
  computer,
  NotFound,
  ForegroundConsentRequired,
  Unavailable,
}} from "@avibe/computer";

export default async () => {{
  const types = {{ NotFound, ForegroundConsentRequired, Unavailable }};
  const mail = await computer.app({{ bundleId: "com.apple.mail" }});
  const window = await mail.window({{ labelContains: "New Message" }});
  try {{
    await window
      .element({{ role: "button", label: "Send", index: 0 }})
      .click();
  }} catch (error) {{
    return {{ typed: error instanceof types.{error_class}, code: error.code }};
  }}
  throw new Error("typed Controller error was not raised");
}};
""",
        click_error=controller_code,
    )

    assert payload == {
        "ok": True,
        "result": {"typed": True, "code": controller_code},
    }


def test_sdk_rejects_an_ambiguous_within_boundary(tmp_path: Path) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window
    .element({
      role: "button",
      label: "Send",
      within: { role: "group", label: "Composer" },
    })
    .click();
  return {};
};
""",
        duplicate_within=True,
    )

    assert payload["ok"] is False
    assert payload["error"]["name"] == "Ambiguous"
    assert payload["error"]["step"] == "element.click"
    assert all(
        request["payload"].get("tool") != "click"
        for request in requests
        if request["path"].endswith("/request")
    )


def test_sdk_reobserves_before_each_action_and_rejects_new_ambiguity(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.element({ role: "button", label: "Add" }).click();
  await window.element({ role: "button", label: "Delete" }).click();
  return {};
};
""",
        dynamic_elements_after_action=True,
    )

    assert payload["ok"] is False
    assert payload["error"]["name"] == "Ambiguous"
    assert payload["error"]["step"] == "element.click"
    click_requests = [
        request
        for request in requests
        if request["payload"].get("tool") == "click"
    ]
    assert len(click_requests) == 2
    assert {
        request["payload"]["arguments"]["element_token"]
        for request in click_requests
    } == {"add-1", "add-2"}


def test_sdk_action_ignores_a_cached_screenshot_only_observation(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.observe({ include_accessibility_tree: false });
  const effect = await window
    .element({ role: "button", label: "Send", index: 0 })
    .click();
  return { effect: effect.status };
};
""",
    )

    assert payload == {
        "ok": True,
        "result": {"effect": "confirmed"},
    }
    observations = [
        request
        for request in requests
        if request["payload"].get("tool") == "get_window_state"
    ]
    assert observations[0]["payload"]["arguments"][
        "include_accessibility_tree"
    ] is False
    assert len(observations) == 3


@pytest.mark.parametrize(
    ("source", "expected_step"),
    [
        (
            """
import { computer } from "@avibe/computer";
export default async () => {
  await computer.app({ bundleId: "com.apple.mail", text: "Mail" });
};
""",
            "computer.app",
        ),
        (
            """
import { computer } from "@avibe/computer";
export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  await mail.window({ labelContains: "New Message", name: "New Message" });
};
""",
            "app.window",
        ),
        (
            """
import { computer } from "@avibe/computer";
export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.element({ role: "button", name: "Send" }).click();
};
""",
            "element.click",
        ),
    ],
)
def test_sdk_rejects_unknown_selector_fields_without_acting(
    tmp_path: Path,
    source: str,
    expected_step: str,
) -> None:
    payload, _server, requests = _run_node_script(tmp_path, source)

    assert payload["ok"] is False
    assert payload["error"]["name"] == "ControllerError"
    assert payload["error"]["code"] == "invalid_selector"
    assert payload["error"]["step"] == expected_step
    assert all(
        request["payload"].get("tool") != "click"
        for request in requests
        if request["path"].endswith("/request")
    )


def test_sdk_wait_does_not_turn_an_invalid_selector_into_a_timeout(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  await computer.waitFor(
    { bundleId: "com.apple.mail", text: "Mail" },
    { timeout: 1000 },
  );
};
""",
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_selector"
    assert payload["error"]["step"] == "computer.waitFor"


def test_sdk_expect_does_not_turn_an_invalid_index_into_a_timeout(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window
    .expect({ role: "button", index: -1 })
    .toExist({ timeout: 1000 });
};
""",
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_selector"
    assert payload["error"]["step"] == "expect.toExist"


def test_sdk_zero_timeout_checks_each_current_state_once(tmp_path: Path) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.waitFor(
    { bundleId: "com.apple.mail" },
    { timeout: 0 },
  );
  const window = await mail.waitFor(
    { labelContains: "New Message" },
    { timeout: 0 },
  );
  await window
    .expect({ role: "textbox", label: "Subject" })
    .toHaveValue("Quarterly report", { timeout: 0 });
  return { pid: mail.pid, windowId: window.windowId };
};
""",
    )

    assert payload == {
        "ok": True,
        "result": {"pid": 41, "windowId": 9},
    }
    tools = [
        request["payload"].get("tool")
        for request in requests
        if request["path"].endswith("/request")
    ]
    assert tools.count("list_apps") == 1
    assert tools.count("list_windows") == 1
    assert tools.count("get_window_state") == 1


def test_sdk_selected_app_descriptor_keeps_the_selected_positive_pid(
    tmp_path: Path,
) -> None:
    apps = [
        {
            "pid": pid,
            "name": "Mail",
            "bundle_id": "com.apple.mail",
            "running": True,
            "active": pid == 42,
        }
        for pid in (41, 42)
    ]
    windows = [
        {
            "pid": 42,
            "window_id": 10,
            "app_name": "Mail",
            "title": "Selected Process",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
            "z_index": 1,
        }
    ]
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({
    bundleId: "com.apple.mail",
    index: 1,
  });
  const window = await mail.window({ label: "Selected Process" });
  return { appPid: mail.pid, windowPid: window.pid };
};
""",
        apps=apps,
        windows=windows,
    )

    assert payload == {
        "ok": True,
        "result": {"appPid": 42, "windowPid": 42},
    }
    list_window_requests = [
        request
        for request in requests
        if request["payload"].get("tool") == "list_windows"
    ]
    assert [request["payload"]["arguments"] for request in list_window_requests] == [
        {"pid": 42}
    ]
    assert list_window_requests[0]["payload"]["descriptor"] == {
        "kind": "app",
        "pid": 42,
        "bundle_id": "com.apple.mail",
        "name": None,
    }


def test_sdk_app_handle_never_refreshes_or_rebinds_before_an_action(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  await mail.forceQuit();
};
""",
    )

    assert payload == {"ok": True, "result": None}
    tool_requests = [
        request
        for request in requests
        if request["path"].endswith("/request")
    ]
    assert [request["payload"]["tool"] for request in tool_requests] == [
        "list_apps",
        "kill_app",
    ]
    assert tool_requests[-1]["payload"]["descriptor"] == {
        "kind": "app",
        "pid": 41,
        "bundle_id": "com.apple.mail",
        "name": None,
    }


def test_sdk_window_handle_never_refreshes_or_rebinds_its_application(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.observe({ include_screenshot: false });
};
""",
    )

    assert payload == {"ok": True, "result": None}
    tool_requests = [
        request
        for request in requests
        if request["path"].endswith("/request")
    ]
    assert [request["payload"]["tool"] for request in tool_requests] == [
        "list_apps",
        "list_windows",
        "get_window_state",
    ]
    assert tool_requests[-1]["payload"]["descriptor"] == {
        "kind": "window",
        "pid": 41,
        "window_id": 9,
        "app": {
            "kind": "app",
            "pid": 41,
            "bundle_id": "com.apple.mail",
            "name": None,
        },
    }


def test_sdk_launch_with_pid_zero_resolves_one_fresh_app_descriptor(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const target = await computer.app({ bundleId: "com.apple.mail" });
  const launched = await target.launch();
  return {
    oldPid: target.pid,
    newPid: launched.app.pid,
    effect: launched.status,
  };
};
""",
        app_responses=[
            [
                {
                    "pid": 0,
                    "name": "Mail",
                    "bundle_id": "com.apple.mail",
                    "running": False,
                    "active": False,
                }
            ],
            [
                {
                    "pid": 57,
                    "name": "Mail",
                    "bundle_id": "com.apple.mail",
                    "running": True,
                    "active": True,
                }
            ],
        ],
        launch_pid=0,
    )

    assert payload == {
        "ok": True,
        "result": {
            "oldPid": None,
            "newPid": 57,
            "effect": "confirmed",
        },
    }
    tool_requests = [
        request
        for request in requests
        if request["path"].endswith("/request")
    ]
    assert [request["payload"]["tool"] for request in tool_requests] == [
        "list_apps",
        "launch_app",
        "list_apps",
    ]
    assert tool_requests[1]["payload"]["descriptor"] == {
        "kind": "app",
        "pid": None,
        "bundle_id": "com.apple.mail",
        "name": None,
    }


def test_sdk_element_double_click_uses_the_dedicated_tool(tmp_path: Path) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  const effect = await window
    .element({ role: "button", label: "Send", index: 0 })
    .doubleClick();
  return effect.status;
};
""",
    )

    assert payload == {"ok": True, "result": "confirmed"}
    action_requests = [
        request
        for request in requests
        if request["payload"].get("tool") in {"click", "double_click"}
    ]
    assert [request["payload"]["tool"] for request in action_requests] == [
        "double_click",
    ]
    assert "count" not in action_requests[0]["payload"]["arguments"]


def test_sdk_window_index_uses_descending_z_order(tmp_path: Path) -> None:
    windows = [
        {
            "pid": 41,
            "window_id": 9,
            "app_name": "Mail",
            "title": "Back",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
            "z_index": 1,
        },
        {
            "pid": 41,
            "window_id": 10,
            "app_name": "Mail",
            "title": "Front",
            "bounds": {"x": 10, "y": 10, "width": 800, "height": 600},
            "is_on_screen": True,
            "z_index": 5,
        },
    ]
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ role: "window", index: 0 });
  return { id: window.windowId, title: window.title };
};
""",
        windows=windows,
    )

    assert payload == {
        "ok": True,
        "result": {"id": 10, "title": "Front"},
    }


def test_sdk_window_index_refuses_unobservable_order(tmp_path: Path) -> None:
    windows = [
        {
            "pid": 41,
            "window_id": window_id,
            "app_name": "Mail",
            "title": title,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
            "z_index": z_index,
        }
        for window_id, title, z_index in [(9, "One", None), (10, "Two", 5)]
    ]
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  await mail.window({ role: "window", index: 0 });
};
""",
        windows=windows,
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "window_order_unavailable"
    assert payload["error"]["step"] == "app.window"


def test_runner_returns_null_and_exits_with_user_timers_still_open(
    tmp_path: Path,
) -> None:
    payload, server, _requests = _run_node_script(
        tmp_path,
        """
export default async () => {
  setInterval(() => {}, 1000);
};
""",
    )

    assert payload == {"ok": True, "result": None}
    assert server.subprocess_elapsed < 1.0  # type: ignore[attr-defined]


def test_python_runner_retains_only_a_bounded_diagnostic_tail(
    tmp_path: Path,
) -> None:
    payload, server, _requests = _run_node_script(
        tmp_path,
        """
export default async () => {
  process.stdout.write("discarded-marker\\n");
  process.stdout.write("x".repeat(128 * 1024));
  process.stdout.write("\\nretained-tail\\n");
};
""",
    )

    completed = server.completed  # type: ignore[attr-defined]

    assert payload == {"ok": True, "result": None}
    assert completed.returncode == 0
    assert len(completed.stderr.encode("utf-8")) <= (
        computer_script_runner._SCRIPT_DIAGNOSTIC_TAIL_BYTES
    )
    assert "discarded-marker" not in completed.stderr
    assert completed.stderr.endswith("\nretained-tail\n")


def test_python_runner_rejects_an_oversized_result_with_canonical_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error, requests, elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        f"""
export default async () => "x".repeat(
  {computer_script_runner._SCRIPT_RESULT_MAX_BYTES}
);
""",
        timeout_seconds=5,
    )

    assert error.details == {
        "name": "ControllerError",
        "code": "computer_script_protocol_error",
        "message": "The script result exceeded the protocol size limit.",
        "params": {
            "limit_bytes": computer_script_runner._SCRIPT_RESULT_MAX_BYTES,
        },
        "step": "script",
        "screenshot_path": None,
        "ambiguous": True,
    }
    assert [
        request
        for request in requests
        if request["path"].endswith("/request")
    ] == []
    assert elapsed < 2


def test_python_deadline_is_not_held_by_a_detached_descendant_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error, requests, elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        """
import { spawn } from "node:child_process";

export default async () => {
  const child = spawn(
    process.execPath,
    ["-e", "setTimeout(() => process.exit(0), 2000)"],
    { detached: true, stdio: ["ignore", "inherit", "inherit"] },
  );
  child.unref();
  await new Promise(() => {});
};
""",
    )

    assert error.details == {
        "name": "Timeout",
        "code": "script_timeout",
        "message": "The script deadline expired.",
        "params": {},
        "step": "script",
        "screenshot_path": None,
        "ambiguous": True,
    }
    assert requests == []
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("source", "response_delay_by_tool", "expected_step", "expected_tool"),
    [
        (
            """
export default function () {
  while (true) {}
};
""",
            None,
            "script",
            None,
        ),
        (
            """
export default async () => {
  await new Promise(() => {});
};
""",
            None,
            "script",
            None,
        ),
        (
            """
import { computer } from "@avibe/computer";

export default async () => {
  await computer.clipboard.write("deadline fixture");
};
""",
            {"clipboard_write": 1.5},
            "computer.clipboard.write",
            "clipboard_write",
        ),
    ],
)
def test_python_parent_owns_the_canonical_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    response_delay_by_tool: dict[str, float] | None,
    expected_step: str,
    expected_tool: str | None,
) -> None:
    error, requests, elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        source,
        response_delay_by_tool=response_delay_by_tool,
    )

    assert error.details == {
        "name": "Timeout",
        "code": "script_timeout",
        "message": "The script deadline expired.",
        "params": {},
        "step": expected_step,
        "screenshot_path": None,
        "ambiguous": True,
    }
    tools = [
        request["payload"].get("tool")
        for request in requests
        if request["path"].endswith("/request")
    ]
    if expected_tool is None:
        assert tools == []
    else:
        assert expected_tool in tools
    assert elapsed < 1.0


def test_python_total_deadline_preserves_the_last_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error, requests, _elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.element({ role: "button", label: "Send", index: 0 }).click();
};
""",
        response_delay_by_tool={"click": 2.0},
        timeout_seconds=0.75,
    )

    screenshot_path = error.details["screenshot_path"]
    assert isinstance(screenshot_path, str)
    screenshot = Path(screenshot_path)
    try:
        assert screenshot.read_bytes() == b"png-fixture"
        assert error.details["step"] == "element.click"
        assert error.details["ambiguous"] is True
        assert any(
            request["payload"].get("tool") == "click"
            for request in requests
            if request["path"].endswith("/request")
        )
    finally:
        shutil.rmtree(screenshot.parent, ignore_errors=True)


def test_python_failure_retains_only_the_referenced_screenshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    error, _requests, _elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.observe();
  await window.observe();
  await window.element({ role: "button", label: "Missing" }).click();
};
""",
        timeout_seconds=5,
    )

    screenshot_path = error.details["screenshot_path"]
    assert isinstance(screenshot_path, str)
    screenshot = Path(screenshot_path)
    try:
        assert screenshot.read_bytes() == b"png-fixture"
        assert list(screenshot.parent.iterdir()) == [screenshot]
    finally:
        shutil.rmtree(screenshot.parent, ignore_errors=True)


@pytest.mark.parametrize(
    ("source", "delayed_tool", "expected_step"),
    [
        (
            """
import { computer } from "@avibe/computer";

export default async () => {
  await computer.waitFor(
    { bundleId: "com.apple.mail" },
    { timeout: 100 },
  );
  return {};
};
""",
            "list_apps",
            "computer.waitFor",
        ),
        (
            """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window
    .expect({ role: "textfield", label: "Subject" })
    .toHaveValue("Quarterly report", { timeout: 100 });
  return {};
};
""",
            "get_window_state",
            "expect.toHaveValue",
        ),
    ],
)
def test_sdk_local_timeout_aborts_an_in_flight_controller_request(
    tmp_path: Path,
    source: str,
    delayed_tool: str,
    expected_step: str,
) -> None:
    payload, server, requests = _run_node_script(
        tmp_path,
        source,
        response_delay_by_tool={delayed_tool: 1.5},
    )

    assert payload["ok"] is False
    assert payload["error"]["name"] == "Timeout"
    assert payload["error"]["code"] == "timeout"
    assert payload["error"]["step"] == expected_step
    assert payload["error"]["ambiguous"] is True
    assert server.subprocess_elapsed < 1.0  # type: ignore[attr-defined]
    assert all(
        request["payload"].get("tool") != "click"
        for request in requests
        if request["path"].endswith("/request")
    )


def test_script_arguments_keep_strings_and_load_utf8_files(tmp_path: Path) -> None:
    source = tmp_path / "body.txt"
    source.write_text("hello\n世界\n", encoding="utf-8")
    assert parse_script_arguments(
        [" count =01", f"body=@{source}", "json={\"x\":1}"]
    ) == {
        "count": "01",
        "body": "hello\n世界\n",
        "json": '{"x":1}',
    }


def test_script_arguments_reject_duplicate_names() -> None:
    with pytest.raises(ComputerScriptRunnerError) as exc_info:
        parse_script_arguments(["name=first", "name=second"])
    assert exc_info.value.code == "duplicate_computer_script_argument"


def test_python_runner_uses_one_deadline_for_open_node_and_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = tmp_path / "script.mjs"
    script.write_text("export default async () => ({ ok: true });", encoding="utf-8")
    endpoint = object()
    captured: dict[str, object] = {}
    ticks = iter([100.0, 102.0, 105.0, 106.0])

    monkeypatch.setattr(
        computer_script_runner,
        "private_desktop_node_bin",
        lambda: Path("/private/node"),
    )
    monkeypatch.setattr(
        computer_script_runner,
        "resolve_computer_script_endpoint",
        lambda: endpoint,
    )

    def open_connection(**kwargs):
        captured["open"] = kwargs
        return {
            "body": {
                "ok": True,
                "connection_token": "token",
            }
        }

    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_open_sync",
        open_connection,
    )
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_endpoint_payload",
        lambda value: {"endpoint": value is endpoint},
    )

    def run_node(node, runner, request, timeout_seconds):
        captured["node"] = node
        captured["runner"] = runner
        captured["request"] = request
        captured["timeout_seconds"] = timeout_seconds
        return subprocess.CompletedProcess(
            args=[str(node), str(runner)],
            returncode=0,
            stdout='{"ok":true,"result":{"done":true}}',
            stderr="",
        )

    monkeypatch.setattr(computer_script_runner, "_run_node_process", run_node)
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_close_sync",
        lambda **kwargs: captured.update(close=kwargs),
    )
    monkeypatch.setattr(computer_script_runner.time, "monotonic", lambda: next(ticks))

    assert computer_script_runner.run_computer_script(
        caller_session_id="ses-script",
        script_file=str(script),
        arg_values=[],
        timeout_seconds=20,
    ) == {"done": True}

    assert captured["open"] == {
        "endpoint": endpoint,
        "caller_session_id": "ses-script",
        "timeout_seconds": 20,
        "request_timeout": 10.0,
    }
    assert captured["timeout_seconds"] == 15.0
    request = captured["request"]
    assert isinstance(request, dict)
    assert "timeout_ms" not in request
    assert "deadline_unix_ms" not in request
    assert request["runtime_state_path"].endswith("/runtime-state.json")
    assert captured["close"] == {
        "endpoint": endpoint,
        "connection_token": "token",
        "timeout": 1.0,
    }


@pytest.mark.parametrize(
    ("code", "key"),
    [
        ("active_turn_required", "computer.cli.error.missingCallerTurn"),
        ("session_archived", "computer.cli.error.sessionArchived"),
        ("other_rejection", "computer.cli.error.scriptOpenRejected"),
    ],
)
def test_python_runner_localizes_known_open_rejections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code: str,
    key: str,
) -> None:
    script = tmp_path / "script.mjs"
    script.write_text("export default async () => null;", encoding="utf-8")
    monkeypatch.setattr(
        computer_script_runner,
        "private_desktop_node_bin",
        lambda: Path("/private/node"),
    )
    monkeypatch.setattr(
        computer_script_runner,
        "resolve_computer_script_endpoint",
        lambda: object(),
    )
    monkeypatch.setattr(
        computer_script_runner,
        "computer_script_open_sync",
        lambda **_kwargs: {"body": {"ok": False, "error": code}},
    )

    with pytest.raises(ComputerScriptRunnerError) as exc_info:
        computer_script_runner.run_computer_script(
            caller_session_id="ses-script",
            script_file=str(script),
            arg_values=[],
        )

    assert exc_info.value.code == code
    assert exc_info.value.key == key
    assert exc_info.value.params == (
        {"code": code} if code == "other_rejection" else {}
    )


def test_python_runner_formats_integral_timeout_without_decimal() -> None:
    assert computer_script_runner._display_seconds(300.0) == 300
    assert computer_script_runner._display_seconds(0.25) == 0.25


def test_python_runner_uses_the_verified_controller_endpoint_for_open_and_close() -> None:
    with tempfile.TemporaryDirectory(prefix="avibe-script-uds-", dir="/tmp") as directory:
        socket_path = Path(directory) / "dispatch.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.listen(1)
        socket_path.chmod(0o600)
        captured: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.append((request.url.path, payload))
            if request.url.path.endswith("/open"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "connection_token": "token",
                        "session_id": "ses-script",
                        "timeout_ms": 30000,
                    },
                )
            return httpx.Response(200, json={"ok": True, "closed": True})

        try:
            with patch(
                "vibe.internal_client.httpx.HTTPTransport",
                return_value=httpx.MockTransport(handler),
            ):
                endpoint = resolve_computer_script_endpoint(socket_path)
                opened = computer_script_open_sync(
                    endpoint=endpoint,
                    caller_session_id="ses-script",
                    timeout_seconds=30,
                )
                closed = computer_script_close_sync(
                    endpoint=endpoint,
                    connection_token="token",
                )
        finally:
            listener.close()

    assert computer_script_endpoint_payload(endpoint) == {
        "transport": "unix",
        "socket_path": str(socket_path),
    }
    assert opened["body"]["connection_token"] == "token"
    assert closed["body"]["closed"] is True
    assert captured == [
        (
            "/internal/computer-script/open",
            {"caller_session_id": "ses-script", "timeout_seconds": 30},
        ),
        (
            "/internal/computer-script/close",
            {"connection_token": "token"},
        ),
    ]
