from __future__ import annotations

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
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


# An interaction answer that arrives after the request was sent but cannot be
# decoded, so the client cannot know whether the Controller acted on it.
_GARBLED_RESPONSE = "<garbled-response>"


@contextmanager
def _fake_controller(
    *,
    click_error: str | None = None,
    initial_click_error: str = "observe_first",
    duplicate_within: bool = False,
    dynamic_elements_after_action: bool = False,
    response_delay_by_tool: dict[str, float] | None = None,
    apps: list[dict] | None = None,
    app_responses: list[list[dict]] | None = None,
    windows: list[dict] | None = None,
    launch_pid: int | None = 41,
    interaction_errors: dict[str, str] | None = None,
    observation_screenshot_modes: list[str] | None = None,
) -> Iterator[tuple[ThreadingHTTPServer, list[dict]]]:
    requests: list[dict] = []
    observations = 0
    clicks = 0
    app_lookups = 0
    connection_ids: set[int] = set()
    driver_has_screenshot = False

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            nonlocal observations, clicks, app_lookups, driver_has_screenshot
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
            elif self.path in {
                "/internal/computer-script/notify",
                "/internal/computer-script/confirm",
            }:
                operation = self.path.rsplit("/", 1)[-1]
                error = (interaction_errors or {}).get(operation)
                if error == _GARBLED_RESPONSE:
                    body = None
                elif error is not None:
                    body = {
                        "ok": False,
                        "error": error,
                        "params": {},
                        "ambiguous": False,
                        "dispatched": False,
                    }
                else:
                    body = {
                        "ok": True,
                        "result": {
                            "status": "confirmed",
                            "effect": "confirmed",
                            "route": (
                                "owner_confirm"
                                if operation == "confirm"
                                else "message_dispatcher"
                            ),
                            "delivery": {
                                "mode": "foreground",
                                "delivered_count": 1,
                            },
                        },
                    }
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
                    observation = {
                        "pid": 41,
                        "window_id": 9,
                        **({"elements": elements} if elements is not None else {}),
                    }
                    if observation_screenshot_modes is not None:
                        mode = observation_screenshot_modes[
                            min(observations - 1, len(observation_screenshot_modes) - 1)
                        ]
                    elif payload["arguments"].get("screenshot_out_file"):
                        mode = "file"
                    elif payload["arguments"].get("include_screenshot") is False:
                        mode = "tree"
                    else:
                        mode = "image"
                    if mode == "file":
                        observation["screenshot_file_path"] = payload["arguments"].get(
                            "screenshot_out_file", "/tmp/fixture-window.png"
                        )
                    elif mode == "invalid":
                        observation.update(
                            {
                                "screenshot_frame_valid": False,
                                "screenshot_width": 800,
                                "screenshot_height": 600,
                            }
                        )
                    driver_has_screenshot = mode in {"image", "file"}
                    body = _result(
                        observation,
                        image=mode in {"image", "invalid"},
                    )
                elif tool in {
                    "click",
                    "double_click",
                    "right_click",
                    "drag",
                    "scroll",
                    "type_text",
                    "press_key",
                    "hotkey",
                } and any(
                    field in payload["arguments"]
                    for field in ("x", "y", "from_x", "from_y", "to_x", "to_y")
                ) and not driver_has_screenshot:
                    body = _result(
                        {
                            "code": "screenshot_context_missing",
                            "params": {},
                            "dispatched": False,
                        },
                        is_error=True,
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
                            {
                                "code": initial_click_error,
                                "params": {},
                                "dispatched": False,
                            },
                            is_error=True,
                        )
                    else:
                        body = _result(
                            {
                                "effect": "confirmed",
                                "route": "accessibility",
                            }
                        )
                elif tool in {"double_click", "press_key", "drag"}:
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
            encoded = b"{" if body is None else json.dumps(body).encode()
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
    initial_click_error: str = "observe_first",
    duplicate_within: bool = False,
    dynamic_elements_after_action: bool = False,
    response_delay_by_tool: dict[str, float] | None = None,
    apps: list[dict] | None = None,
    app_responses: list[list[dict]] | None = None,
    windows: list[dict] | None = None,
    launch_pid: int | None = 41,
    interaction_errors: dict[str, str] | None = None,
    observation_screenshot_modes: list[str] | None = None,
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
    result_path = artifacts / "result.json"

    with _fake_controller(
        click_error=click_error,
        initial_click_error=initial_click_error,
        duplicate_within=duplicate_within,
        dynamic_elements_after_action=dynamic_elements_after_action,
        response_delay_by_tool=response_delay_by_tool,
        apps=apps,
        app_responses=app_responses,
        windows=windows,
        launch_pid=launch_pid,
        interaction_errors=interaction_errors,
        observation_screenshot_modes=observation_screenshot_modes,
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
            "result_path": str(result_path),
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
        result_read = computer_script_runner._read_capped_json(
            result_path,
            computer_script_runner._SCRIPT_RESULT_MAX_BYTES,
        )
        assert result_read.error is None
        assert isinstance(result_read.value, dict)
        return result_read.value, server, requests


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


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.02)
    pytest.fail(f"process {pid} survived the script supervisor")


def _blocking_node_runner(tmp_path: Path) -> tuple[Path, Path]:
    pid_path = tmp_path / "node.pid"
    runner = tmp_path / "blocking-runner.mjs"
    runner.write_text(
        (
            'import fs from "node:fs";\n'
            f"fs.writeFileSync({json.dumps(str(pid_path))}, String(process.pid));\n"
            "while (true) {}\n"
        ),
        encoding="utf-8",
    )
    return runner, pid_path


def _assert_supervisor_wait_failure_kills_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    if NODE is None:
        pytest.skip("Node.js is unavailable")
    if os.name != "posix":
        pytest.skip("process-group supervision is POSIX-only")

    runner, pid_path = _blocking_node_runner(tmp_path)
    real_popen = subprocess.Popen
    captured: dict[str, subprocess.Popen] = {}

    class FailingWaitProcess:
        def __init__(self, *args, **kwargs) -> None:
            self._process = real_popen(*args, **kwargs)
            self.pid = self._process.pid
            self._failed = False
            captured["process"] = self._process

        def wait(self, timeout=None):
            if not self._failed:
                self._failed = True
                deadline = time.monotonic() + 3
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert pid_path.exists()
                raise failure
            return self._process.wait(timeout=timeout)

        def poll(self):
            return self._process.poll()

        def kill(self) -> None:
            self._process.kill()

    monkeypatch.setattr(
        computer_script_runner.subprocess,
        "Popen",
        FailingWaitProcess,
    )
    request = {"result_path": str(tmp_path / "result.json")}

    with pytest.raises(type(failure)):
        computer_script_runner._run_node_process(
            Path(NODE),
            runner,
            request,
            30,
        )

    node_pid = int(pid_path.read_text(encoding="utf-8"))
    assert captured["process"].poll() is not None
    _wait_for_pid_exit(node_pid)


def test_keyboard_interrupt_during_wait_kills_the_node_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_supervisor_wait_failure_kills_node(
        monkeypatch,
        tmp_path,
        KeyboardInterrupt(),
    )


def test_supervisor_exception_during_wait_kills_the_node_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_supervisor_wait_failure_kills_node(
        monkeypatch,
        tmp_path,
        RuntimeError("supervisor failed"),
    )


def test_sigterm_to_the_supervisor_kills_the_node_process(tmp_path: Path) -> None:
    if NODE is None:
        pytest.skip("Node.js is unavailable")
    if os.name != "posix":
        pytest.skip("SIGTERM supervision is POSIX-only")

    runner, pid_path = _blocking_node_runner(tmp_path)
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "from vibe.computer_script_runner import _run_node_process",
                (
                    f"_run_node_process(Path({str(NODE)!r}), "
                    f"Path({str(runner)!r}), "
                    f"{{'result_path': {str(tmp_path / 'result.json')!r}}}, 30)"
                ),
            ]
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path.cwd()), environment.get("PYTHONPATH", "")])
    )
    process = subprocess.Popen(
        [sys.executable, str(supervisor)],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    node_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        assert pid_path.exists()
        node_pid = int(pid_path.read_text(encoding="utf-8"))

        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) != 0
        _wait_for_pid_exit(node_pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        if node_pid is not None and _pid_exists(node_pid):
            os.kill(node_pid, signal.SIGKILL)


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
        initial_click_error="stale_element_token",
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


def test_avibe_notify_and_confirm_return_effects(tmp_path: Path) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { avibe } from "@avibe/computer";

export default async () => ({
  notify: await avibe.notify("Progress"),
  confirm: await avibe.confirm("Continue?"),
});
""",
    )

    assert payload == {
        "ok": True,
        "result": {
            "notify": {
                "status": "confirmed",
                "effect": "confirmed",
                "route": "message_dispatcher",
                "delivery": {
                    "mode": "foreground",
                    "delivered_count": 1,
                },
            },
            "confirm": {
                "status": "confirmed",
                "effect": "confirmed",
                "route": "owner_confirm",
                "delivery": {
                    "mode": "foreground",
                    "delivered_count": 1,
                },
            },
        },
    }
    assert [
        request["path"]
        for request in requests
        if request["path"].startswith("/internal/computer-script/")
    ] == [
        "/internal/computer-script/notify",
        "/internal/computer-script/confirm",
        "/internal/computer-script/close",
    ]


def test_avibe_confirm_refusal_is_typed_and_unambiguous(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { ConfirmRefused, avibe } from "@avibe/computer";

export default async () => {
  try {
    await avibe.confirm("Continue?");
  } catch (error) {
    return {
      instance: error instanceof ConfirmRefused,
      code: error.code,
      ambiguous: error.ambiguous,
      step: error.step,
    };
  }
  throw new Error("confirmation unexpectedly succeeded");
};
""",
        interaction_errors={"confirm": "confirm_refused"},
    )

    assert payload == {
        "ok": True,
        "result": {
            "instance": True,
            "code": "confirm_refused",
            "ambiguous": False,
            "step": "avibe.confirm",
        },
    }


@pytest.mark.parametrize(
    ("operation", "ambiguous"),
    [("notify", True), ("confirm", False)],
)
def test_avibe_interaction_lost_response_ambiguity(
    tmp_path: Path, operation: str, ambiguous: bool
) -> None:
    """A notify may already be delivered; a lost confirm never approved anything."""

    payload, _server, _requests = _run_node_script(
        tmp_path,
        f"""
import {{ avibe }} from "@avibe/computer";

export default async () => {{
  try {{
    await avibe.{operation}("Continue?");
  }} catch (error) {{
    return {{ code: error.code, ambiguous: error.ambiguous }};
  }}
  throw new Error("lost response was not raised");
}};
""",
        interaction_errors={operation: _GARBLED_RESPONSE},
    )

    assert payload == {
        "ok": True,
        "result": {"code": "invalid_computer_use_response", "ambiguous": ambiguous},
    }


def test_sdk_failure_reports_step_and_last_screenshot(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  await window.observe();
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


def test_plain_script_error_keeps_last_completed_step_separate(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  await computer.app({ bundleId: "com.apple.mail" });
  throw new Error("plain script failure");
};
""",
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "computer_script_failed"
    assert payload["error"]["step"] == "script"
    assert payload["error"]["last_completed_step"] == "computer.app"
    assert payload["error"]["ambiguous"] is False


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
        "last_completed_step": "computer.app",
        "screenshot_path": str(screenshot_path),
        "ambiguous": True,
    }
    assert tuple(expected) == computer_script_runner._failure_envelope_fields()

    payload, _server, _requests = _run_node_script(
        tmp_path,
        f"""
import {{ computer }} from "@avibe/computer";

export default async () => {{
  await computer.app({{ bundleId: "com.apple.mail" }});
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
    def return_failure(_node, _runner, request, _timeout_seconds):
        Path(request["result_path"]).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        Path(request["runtime_state_path"]).write_text(
            json.dumps(
                {
                    "step": expected["step"],
                    "last_completed_step": expected["last_completed_step"],
                    "screenshot_path": expected["screenshot_path"],
                    "diagnostics": "",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=["node", "runner.mjs"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(
        computer_script_runner,
        "_run_node_process",
        return_failure,
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


def test_sdk_controller_error_ambiguity_table(tmp_path: Path) -> None:
    if NODE is None:
        pytest.skip("Node.js is unavailable")

    not_dispatched_codes = [
        "computer_script_request_too_large",
        "invalid_computer_script_request",
        "active_turn_required",
        "session_archived",
        "script_connection_invalid",
        "script_timeout",
        "script_turn_ended",
        "invalid_computer_script_tool",
        "session_argument_forbidden",
        "computer_use_unavailable",
        "session_required",
        "unknown_tool",
        "invalid_arguments",
        "invalid_computer_script_descriptor",
        "computer_script_descriptor_required",
        "stale_app_handle",
        "stale_window_handle",
        "target_busy",
        "platform_unsupported",
        "foreground_forbidden",
        "focus_shortcut_forbidden",
        "foreground_consent_required",
        "window_target_required",
        "observe_first",
        "session_unavailable",
    ]
    post_send_codes = [
        "upstream_unavailable",
        "upstream_error",
        "upstream_protocol_error",
        "upstream_tool_error",
        "computer_use_error",
        "application_not_found",
        "element_not_found",
        "snapshot_not_found",
        "window_id_not_found",
        "window_not_found",
        "stale_element_token",
        "capture_failed",
        "tool_invocation_failed",
        "controller_unavailable",
        "timeout",
        "invalid_computer_use_response",
    ]
    cases = [
        *[
            {
                "code": code,
                "requestWasSent": True,
                "controllerPayload": {"dispatched": False},
                "expected": False,
            }
            for code in not_dispatched_codes
        ],
        *[
            {
                "code": code,
                "requestWasSent": True,
                "controllerPayload": {},
                "expected": True,
            }
            for code in post_send_codes
        ],
        {
            "code": "timeout",
            "requestWasSent": False,
            "controllerPayload": {},
            "expected": False,
        },
        {
            "code": "controller_unavailable",
            "requestWasSent": False,
            "controllerPayload": {},
            "expected": False,
        },
    ]
    script = tmp_path / "ambiguity-table.mjs"
    script.write_text(
        (
            "import { controllerFailureAmbiguous } from "
            f"{json.dumps((SDK_DIRECTORY / 'runtime.mjs').as_uri())};\n"
            f"const cases = {json.dumps(cases)};\n"
            "const actual = cases.map((item) => ({\n"
            "  code: item.code,\n"
            "  expected: item.expected,\n"
            "  actual: controllerFailureAmbiguous(\n"
            "    item.requestWasSent,\n"
            "    item.controllerPayload,\n"
            "  ),\n"
            "}));\n"
            "process.stdout.write(JSON.stringify(actual));\n"
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [NODE, str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(completed.stdout)
    assert all(row["actual"] == row["expected"] for row in actual), actual

    runtime_source = (SDK_DIRECTORY / "runtime.mjs").read_text(encoding="utf-8")
    assert runtime_source.count("new ControllerFault(") == 1
    assert runtime_source.count("controllerPayload.dispatched === false") == 1
    assert "hasAmbiguousInFlightRequest" not in runtime_source


@pytest.mark.parametrize(
    "controller_code",
    ["stale_element_token", "upstream_unavailable"],
)
def test_sdk_never_retries_a_mutation_without_explicit_not_dispatched(
    tmp_path: Path,
    controller_code: str,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        f"""
import {{ computer }} from "@avibe/computer";

export default async () => {{
  const mail = await computer.app({{ bundleId: "com.apple.mail" }});
  const window = await mail.window({{ labelContains: "New Message" }});
  try {{
    await window
      .element({{ role: "button", label: "Send", index: 0 }})
      .click();
  }} catch (error) {{
    return {{ code: error.code, ambiguous: error.ambiguous }};
  }}
  throw new Error("Controller failure was not raised");
}};
""",
        click_error=controller_code,
    )

    assert payload == {
        "ok": True,
        "result": {"code": controller_code, "ambiguous": True},
    }
    assert [
        request["payload"]["tool"]
        for request in requests
        if request["path"].endswith("/request")
        and request["payload"].get("tool") == "click"
    ] == ["click"]


def test_sdk_has_one_mutating_action_retry_gate() -> None:
    source = (SDK_DIRECTORY / "index.mjs").read_text(encoding="utf-8")

    assert source.count("function shouldRetryElementAction(error)") == 1
    assert source.count("shouldRetryElementAction(error)") == 2
    assert source.count("STALE_ELEMENT_CODES.has(error.code)") == 1
    assert source.count("return effectResult(await invoke());") == 2


def test_sdk_marks_post_forward_upstream_failure_as_ambiguous(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  try {
    await window
      .element({ role: "button", label: "Send", index: 0 })
      .click();
  } catch (error) {
    return { code: error.code, ambiguous: error.ambiguous };
  }
  throw new Error("upstream_unavailable was not raised");
};
""",
        click_error="upstream_unavailable",
    )

    assert payload == {
        "ok": True,
        "result": {
            "code": "upstream_unavailable",
            "ambiguous": True,
        },
    }


def test_sdk_marks_post_send_response_decode_failure_as_ambiguous(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  await computer.app({ index: 0 });
};
""",
        apps=[
            {
                "pid": 41,
                "name": "",
                "bundle_id": "",
                "running": True,
                "active": False,
            }
        ],
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_computer_use_response"
    assert payload["error"]["ambiguous"] is True


def test_sdk_maps_stable_controller_codes_to_typed_errors(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer, TargetBusy } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  try {
    await window
      .element({ role: "button", label: "Send", index: 0 })
      .click();
  } catch (error) {
    return { typed: error instanceof TargetBusy, code: error.code };
  }
  throw new Error("target_busy was not raised");
};
""",
        click_error="target_busy",
    )

    assert payload == {
        "ok": True,
        "result": {"typed": True, "code": "target_busy"},
    }


def test_sdk_keeps_the_released_desktop_busy_alias(tmp_path: Path) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer, DesktopBusy, TargetBusy } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ labelContains: "New Message" });
  try {
    await window
      .element({ role: "button", label: "Send", index: 0 })
      .click();
  } catch (error) {
    return {
      desktopAlias: error instanceof DesktopBusy,
      targetType: error instanceof TargetBusy,
      code: error.code,
    };
  }
  throw new Error("desktop_busy was not raised");
};
""",
        click_error="desktop_busy",
    )

    assert payload == {
        "ok": True,
        "result": {
            "desktopAlias": True,
            "targetType": True,
            "code": "target_busy",
        },
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


def _qq_windows() -> list[dict]:
    """The live QQ shape: four windows titled "QQ", only one on screen."""
    return [
        {
            "pid": 41,
            "window_id": window_id,
            "app_name": "QQ",
            "title": "QQ",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": window_id == 12,
            "z_index": z_index,
        }
        for window_id, z_index in [(10, None), (11, None), (12, 3), (13, None)]
    ]


def test_sdk_window_on_screen_resolves_off_screen_title_siblings(
    tmp_path: Path,
) -> None:
    payload, _server, _requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const qq = await computer.app({ bundleId: "com.apple.mail" });
  let ambiguous;
  try {
    await qq.window({ title: "QQ" });
  } catch (error) {
    ambiguous = { name: error.name, count: error.params.count };
  }
  const visible = await qq.window({ title: "QQ", onScreen: true });
  const waited = await qq.waitFor({ title: "QQ", onScreen: true }, 0);
  const hidden = await qq.window({ title: "QQ", onScreen: false, index: 0 })
    .catch((error) => error.code);
  return { ambiguous, visible: visible.windowId, waited: waited.windowId, hidden };
};
""",
        windows=_qq_windows(),
    )

    assert payload == {
        "ok": True,
        "result": {
            "ambiguous": {"name": "Ambiguous", "count": 4},
            "visible": 12,
            "waited": 12,
            # Three off-screen siblings without a stacking order stay unordered.
            "hidden": "window_order_unavailable",
        },
    }


def test_sdk_window_id_selects_one_exact_window(tmp_path: Path) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const qq = await computer.app({ bundleId: "com.apple.mail" });
  const exact = await qq.window({ windowId: 11 });
  const missing = await qq.window({ windowId: 99 }).catch((error) => error.name);
  const mismatch = await qq
    .window({ windowId: 11, onScreen: true })
    .catch((error) => error.name);
  return { exact: [exact.windowId, exact.title], missing, mismatch };
};
""",
        windows=_qq_windows(),
    )

    assert payload == {
        "ok": True,
        "result": {
            "exact": [11, "QQ"],
            "missing": "NotFound",
            "mismatch": "NotFound",
        },
    }
    assert {
        json.dumps(request["payload"]["arguments"], sort_keys=True)
        for request in requests
        if request["payload"].get("tool") == "list_windows"
    } == {json.dumps({"pid": 41})}


@pytest.mark.parametrize(
    ("selector", "field"),
    [
        ('{ windowId: "11" }', "windowId"),
        ("{ windowId: -1 }", "windowId"),
        ("{ windowId: 1.5 }", "windowId"),
        ('{ onScreen: "yes" }', "onScreen"),
        ("{ onScreen: 1 }", "onScreen"),
    ],
)
def test_sdk_rejects_malformed_window_identity_selectors(
    tmp_path: Path,
    selector: str,
    field: str,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        f"""
import {{ computer }} from "@avibe/computer";

export default async () => {{
  const qq = await computer.app({{ bundleId: "com.apple.mail" }});
  await qq.window({selector});
}};
""",
        windows=_qq_windows(),
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_selector"
    assert payload["error"]["params"] == {"field": field}
    assert payload["error"]["step"] == "app.window"
    assert all(request["payload"].get("tool") != "list_windows" for request in requests)


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


def test_runtime_state_retains_only_a_bounded_console_diagnostic_tail(
    tmp_path: Path,
) -> None:
    payload, server, _requests = _run_node_script(
        tmp_path,
        """
export default async () => {
  console.log("discarded-marker");
  console.log("x".repeat(128 * 1024));
  console.log("retained-tail");
};
""",
    )

    completed = server.completed  # type: ignore[attr-defined]
    state_read = computer_script_runner._read_capped_json(
        tmp_path / "artifacts" / "runtime-state.json",
        computer_script_runner._SCRIPT_RUNTIME_STATE_MAX_BYTES,
    )

    assert payload == {"ok": True, "result": None}
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert state_read.error is None
    assert isinstance(state_read.value, dict)
    diagnostics = state_read.value["diagnostics"]
    assert len(diagnostics.encode("utf-8")) <= (
        computer_script_runner._SCRIPT_DIAGNOSTIC_TAIL_BYTES
    )
    assert "discarded-marker" not in diagnostics
    assert diagnostics.endswith("retained-tail\n")
    assert {path.name for path in (tmp_path / "artifacts").iterdir()} == {
        "result.json",
        "runtime-state.json",
    }
    assert (
        tmp_path / "artifacts" / "runtime-state.json"
    ).stat().st_size <= computer_script_runner._SCRIPT_RUNTIME_STATE_MAX_BYTES


def test_continuous_console_logs_leave_no_capture_files_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    real_mkdtemp = tempfile.mkdtemp
    created_directories: list[Path] = []

    def tracked_mkdtemp(*args, **kwargs):
        kwargs["dir"] = temp_root
        created = Path(real_mkdtemp(*args, **kwargs))
        created_directories.append(created)
        return str(created)

    monkeypatch.setattr(computer_script_runner.tempfile, "mkdtemp", tracked_mkdtemp)

    error, _requests, elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        """
export default function () {
  while (true) {
    console.log("x".repeat(4096));
  }
};
""",
    )

    diagnostics = error.details["params"]["diagnostics"]
    assert len(diagnostics.encode("utf-8")) <= (
        computer_script_runner._SCRIPT_DIAGNOSTIC_TAIL_BYTES
    )
    assert elapsed < 1.0
    assert created_directories
    assert all(not path.exists() for path in created_directories)
    assert list(temp_root.iterdir()) == []


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
        "last_completed_step": None,
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
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    real_mkdtemp = tempfile.mkdtemp
    created_directories: list[Path] = []

    def tracked_mkdtemp(*args, **kwargs):
        kwargs["dir"] = temp_root
        created = Path(real_mkdtemp(*args, **kwargs))
        created_directories.append(created)
        return str(created)

    monkeypatch.setattr(computer_script_runner.tempfile, "mkdtemp", tracked_mkdtemp)

    error, requests, elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        """
import { spawn } from "node:child_process";

export default async () => {
  const child = spawn(
    process.execPath,
    [
      "-e",
      "const end=Date.now()+750; const chunk='x'.repeat(8192); " +
        "while(Date.now()<end){process.stdout.write(chunk)}",
    ],
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
        "last_completed_step": None,
        "screenshot_path": None,
        "ambiguous": True,
    }
    assert requests == []
    assert elapsed < 1.0
    assert created_directories
    assert all(not path.exists() for path in created_directories)
    assert list(temp_root.iterdir()) == []


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
        "last_completed_step": None,
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
  await window.observe();
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
        assert error.details["last_completed_step"] == "window.observe"
        assert error.details["ambiguous"] is True
        assert any(
            request["payload"].get("tool") == "click"
            for request in requests
            if request["path"].endswith("/request")
        )
    finally:
        shutil.rmtree(screenshot.parent, ignore_errors=True)


@pytest.mark.parametrize(
    ("termination", "expected_reason"),
    [
        ('process.kill(process.pid, "SIGKILL");', "missing"),
        (
            'fs.writeFileSync(process.env.AVIBE_COMPUTER_RESULT_PATH, "{bad"); '
            "process.exit(7);",
            "invalid_json",
        ),
        (
            "fs.rmSync(process.env.AVIBE_COMPUTER_RESULT_PATH, { force: true }); "
            "process.exit(8);",
            "missing",
        ),
    ],
)
def test_finalize_recovers_step_and_screenshot_from_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    termination: str,
    expected_reason: str,
) -> None:
    error, _requests, _elapsed = _run_python_deadline_script(
        monkeypatch,
        tmp_path,
        f"""
import fs from "node:fs";
import {{ computer }} from "@avibe/computer";

export default async () => {{
  const mail = await computer.app({{ bundleId: "com.apple.mail" }});
  const window = await mail.window({{ labelContains: "New Message" }});
  await window.observe();
  {termination}
}};
""",
        timeout_seconds=5,
    )

    assert error.details["code"] == "computer_script_protocol_error"
    assert error.details["step"] == "script"
    assert error.details["last_completed_step"] == "window.observe"
    assert error.details["params"]["reason"] == expected_reason
    screenshot_path = error.details["screenshot_path"]
    assert isinstance(screenshot_path, str)
    screenshot = Path(screenshot_path)
    try:
        assert screenshot.read_bytes() == b"png-fixture"
        assert list(screenshot.parent.iterdir()) == [screenshot]
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


def test_failure_artifact_retention_is_bounded(tmp_path: Path) -> None:
    retained: list[Path] = []
    for index in range(computer_script_runner._MAX_RETAINED_ARTIFACT_RUNS + 3):
        directory = (
            tmp_path
            / f"{computer_script_runner._ARTIFACT_DIRECTORY_PREFIX}{index:02d}"
        )
        directory.mkdir()
        (directory / "failure.png").write_bytes(b"png")
        os.utime(directory, (index + 1, index + 1))
        retained.append(directory)

    computer_script_runner._prune_retained_artifact_runs(tmp_path)

    survivors = [directory for directory in retained if directory.exists()]
    assert len(survivors) == computer_script_runner._MAX_RETAINED_ARTIFACT_RUNS
    assert survivors == retained[-computer_script_runner._MAX_RETAINED_ARTIFACT_RUNS :]


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
        Path(request["result_path"]).write_text(
            '{"ok":true,"result":{"done":true}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=[str(node), str(runner)],
            returncode=0,
            stdout="",
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
    assert request["result_path"].endswith("/result.json")
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


def _tool_requests(requests: list[dict]) -> list[tuple[str, dict]]:
    return [
        (request["payload"]["tool"], request["payload"]["arguments"])
        for request in requests
        if request["path"].endswith("/request")
    ]


def test_sdk_element_resolution_reads_the_tree_without_a_screenshot(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.element({ role: "button", label: "Send", index: 0 }).doubleClick();
  await window.expect({ role: "textbox", label: "Subject" }).toExist();
  await window.observe();
};
""",
    )

    assert payload == {"ok": True, "result": None}
    observations = [
        arguments
        for tool, arguments in _tool_requests(requests)
        if tool == "get_window_state"
    ]
    assert len(observations) == 3
    # Element resolution (the action and the expectation) skips the capture.
    assert [arguments.get("include_screenshot") for arguments in observations[:2]] == [
        False,
        False,
    ]
    # An explicit observation keeps the driver default, which includes it.
    assert "include_screenshot" not in observations[2]


def test_sdk_pixel_window_input_after_element_resolution_recaptures(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.element({ role: "button", label: "Send", index: 0 }).doubleClick();
  await window.press("Return");
  await window.drag({ from_x: 10, from_y: 20, to_x: 30, to_y: 40 });
  await window.drag({ from_x: 11, from_y: 21, to_x: 31, to_y: 41 });
};
""",
    )

    assert payload == {"ok": True, "result": None}
    sequence = [
        (tool, arguments.get("include_screenshot"))
        for tool, arguments in _tool_requests(requests)
        if tool in {"get_window_state", "double_click", "press_key", "drag"}
    ]
    assert sequence == [
        # Resolution leaves a tree-only snapshot ...
        ("get_window_state", False),
        ("double_click", None),
        # ... which keyboard input can use as it is ...
        ("press_key", None),
        # ... while pixel input first recaptures once, then reuses it.
        ("get_window_state", None),
        ("drag", None),
        ("drag", None),
    ]


def test_sdk_tree_only_requested_capture_recaptures_before_pixel_input(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.observe({ include_screenshot: true });
  await window.drag({ from_x: 10, from_y: 20, to_x: 30, to_y: 40 });
};
""",
        observation_screenshot_modes=["tree", "image"],
    )

    assert payload == {"ok": True, "result": None}
    assert [tool for tool, _arguments in _tool_requests(requests) if tool in {"get_window_state", "drag"}] == [
        "get_window_state",
        "get_window_state",
        "drag",
    ]


def test_sdk_invalid_screenshot_frame_is_not_reused_for_pixel_input(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.observe({ include_screenshot: true });
  await window.drag({ from_x: 10, from_y: 20, to_x: 30, to_y: 40 });
};
""",
        observation_screenshot_modes=["invalid", "image"],
    )

    assert payload == {"ok": True, "result": None}
    sequence = [
        (tool, arguments.get("include_screenshot"))
        for tool, arguments in _tool_requests(requests)
        if tool in {"get_window_state", "drag"}
    ]
    assert sequence == [
        ("get_window_state", True),
        ("get_window_state", None),
        ("drag", None),
    ]


@pytest.mark.parametrize("observation_mode", ["tree", "invalid"])
def test_sdk_missing_screenshots_retry_once_per_pixel_action_and_stay_bounded(
    tmp_path: Path,
    observation_mode: str,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.observe({ include_screenshot: true });
  const errors = [];
  for (const x of [10, 11]) {
    try {
      await window.drag({ from_x: x, from_y: 20, to_x: 30, to_y: 40 });
    } catch (error) {
      errors.push(error.code);
    }
  }
  return { errors };
};
""",
        observation_screenshot_modes=[observation_mode] * 3,
    )

    assert payload == {
        "ok": True,
        "result": {"errors": ["screenshot_context_missing", "screenshot_context_missing"]},
    }
    sequence = [tool for tool, _arguments in _tool_requests(requests) if tool in {"get_window_state", "drag"}]
    assert sequence == [
        "get_window_state",
        "get_window_state",
        "drag",
        "get_window_state",
        "drag",
    ]


def test_sdk_file_screenshot_output_is_reused_for_pixel_input(
    tmp_path: Path,
) -> None:
    payload, _server, requests = _run_node_script(
        tmp_path,
        """
import { computer } from "@avibe/computer";

export default async () => {
  const mail = await computer.app({ bundleId: "com.apple.mail" });
  const window = await mail.window({ label: "New Message" });
  await window.observe({ screenshot_out_file: "/tmp/fixture-window.png" });
  await window.drag({ from_x: 10, from_y: 20, to_x: 30, to_y: 40 });
  await window.drag({ from_x: 11, from_y: 21, to_x: 31, to_y: 41 });
};
""",
        observation_screenshot_modes=["file"],
    )

    assert payload == {"ok": True, "result": None}
    sequence = [tool for tool, _arguments in _tool_requests(requests) if tool in {"get_window_state", "drag"}]
    assert sequence == ["get_window_state", "drag", "drag"]
