"""Hermetic tests for the persistent ``vibe computer js`` kernel.

Each test runs the real internal server on a private Unix socket, the real
script-connection manager, and the shipped Node kernel. Only the desktop
driver (``ComputerUseServer``) and the Computer Use status are fakes, so no
test can reach the owner's desktop, TCC state, or running service.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest

from core import internal_server, session_turns
from core.computer_js_kernels import ComputerJsKernelManager
from core.computer_script_sessions import ComputerScriptSessionManager
from modules.im import MessageContext


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")
SESSION = "ses-js"


def _context(turn_id: str) -> MessageContext:
    return MessageContext(
        user_id="owner",
        channel_id="channel",
        platform="avibe",
        platform_specific={"agent_session_id": SESSION, "turn_token": turn_id},
    )


class _Driver:
    """Fake desktop driver: records every tool call the Controller admits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.image: bytes | None = None

    async def call_tool(self, name: str, arguments: dict, *, handle_descriptor=None) -> dict:
        del handle_descriptor
        self.calls.append((name, arguments))
        payload = {"apps": [{"pid": 41, "bundle_id": "com.apple.mail", "name": "Mail"}]}
        content = []
        if self.image is not None:
            encoded = base64.b64encode(self.image).decode("ascii")
            content.append({"type": "image", "data": encoded, "mimeType": "image/png"})
        return {"isError": False, "structuredContent": payload, "content": content}


class _Harness:
    def __init__(self, controller: SimpleNamespace, client: httpx.AsyncClient) -> None:
        self.controller = controller
        self.client = client
        self.driver: _Driver = controller.computer_use_server
        self.kernels: ComputerJsKernelManager = controller.computer_js_kernels
        self.status = "ready"
        self._turns: list[asyncio.Task[Any]] = []
        self.start_turn("turn-1")

    def start_turn(self, turn_id: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(asyncio.sleep(3600))
        self._turns.append(task)
        self.controller.session_turns.in_flight[SESSION] = session_turns.Turn(
            task=task,
            context=_context(turn_id),
            logical_turn_id=turn_id,
        )
        return task

    async def js(self, code: str, timeout_ms: int = 5000) -> dict[str, Any]:
        response = await self.client.post(
            "/internal/computer-js/eval",
            json={"caller_session_id": SESSION, "code": code, "timeout_ms": timeout_ms},
        )
        return response.json()

    async def reset(self) -> dict[str, Any]:
        response = await self.client.post(
            "/internal/computer-js/reset",
            json={"caller_session_id": SESSION},
        )
        return response.json()

    def kernel_process(self) -> Any:
        return self.kernels._slots[SESSION].kernel.process

    async def close(self) -> None:
        self.kernels.close_all()
        self.controller.computer_script_sessions.close_all()
        for task in self._turns:
            task.cancel()
        await asyncio.gather(*self._turns, return_exceptions=True)
        await asyncio.sleep(0.05)


@asynccontextmanager
async def _harness(**manager_options: Any) -> AsyncIterator[_Harness]:
    import uvicorn

    socket_directory = Path(tempfile.mkdtemp(prefix="avibe-js-", dir="/tmp"))
    socket_path = socket_directory / "c.sock"
    controller = SimpleNamespace(
        computer_use_server=_Driver(),
        controller_id="controller-test",
        _session_id_from_context=lambda context: context.platform_specific["agent_session_id"],
    )
    holder: dict[str, _Harness] = {}
    controller.session_turns = session_turns.SessionTurnManager(controller)
    controller.computer_script_sessions = ComputerScriptSessionManager(controller.session_turns)
    controller.computer_js_kernels = ComputerJsKernelManager(
        controller.computer_script_sessions,
        endpoint_resolver=lambda: {"transport": "unix", "socket_path": str(socket_path)},
        node_resolver=lambda: Path(NODE),
        status_reader=lambda: SimpleNamespace(status=holder["h"].status, reason=None),
        **manager_options,
    )
    # create_app reuses these owners and wires the Turn-end listener.
    app = internal_server.create_app(controller)
    server = uvicorn.Server(uvicorn.Config(app, uds=str(socket_path), log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://controller", timeout=30) as client:
        harness = _Harness(controller, client)
        holder["h"] = harness
        try:
            yield harness
        finally:
            await harness.close()
            server.should_exit = True
            await serving
            shutil.rmtree(socket_directory, ignore_errors=True)


def _run(scenario) -> Any:
    async def main() -> Any:
        async with _harness(**getattr(scenario, "options", {})) as harness:
            return await scenario(harness)

    return asyncio.run(main())


def test_bindings_persist_across_calls_and_report_console_and_freshness() -> None:
    async def scenario(h: _Harness) -> list[dict]:
        return [
            await h.js("const base = await Promise.resolve(40); function add(n) { return base + n }"),
            await h.js("console.log('adding', 2); add(2)"),
            await h.js("let base2 = add(3); ({ base, base2, sum: base + base2 })"),
            await h.js("(await computer.app({ bundleId: 'com.apple.mail' })) !== undefined"),
        ]

    first, second, third, fourth = _run(scenario)
    assert first["ok"] is True and first["kernel_fresh"] is True
    assert second == {
        "ok": True,
        "result": 42,
        "console": "adding 2\n",
        "screenshots": [],
        "kernel_fresh": False,
    }
    assert third["result"] == {"base": 40, "base2": 43, "sum": 83}
    assert fourth["ok"] is True and fourth["result"] is True


def test_sdk_requests_carry_the_exact_turn_binding_and_report_screenshots() -> None:
    async def scenario(h: _Harness) -> tuple[dict, list[tuple[str, dict]], bytes]:
        h.driver.image = b"png-fixture"
        report = await h.js("await computer.app({ bundleId: 'com.apple.mail' }); null")
        return report, h.driver.calls, Path(report["screenshots"][0]).read_bytes()

    report, calls, image = _run(scenario)
    assert calls == [("list_apps", {"session": SESSION})]
    assert len(report["screenshots"]) == 1 and image == b"png-fixture"


def test_js_reset_discards_bindings_and_the_next_call_is_fresh() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict, dict]:
        await h.js("const kept = 1")
        reset = await h.reset()
        after = await h.js("typeof kept")
        again = await h.reset()
        return reset, after, again

    reset, after, again = _run(scenario)
    assert reset == {"ok": True, "discarded": True}
    assert after["result"] == "undefined" and after["kernel_fresh"] is True
    assert again == {"ok": True, "discarded": True}


def test_timeout_cancels_the_call_and_keeps_the_kernel() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict]:
        await h.js("let survivor = 'kept'")
        stuck = await h.js("await new Promise(() => {})", timeout_ms=200)
        after = await h.js("survivor")
        return stuck, after

    stuck, after = _run(scenario)
    assert stuck["ok"] is False
    assert stuck["error"]["code"] == "js_timeout"
    assert stuck["error"]["ambiguous"] is True
    assert stuck["kernel_fresh"] is False
    assert after == {**after, "ok": True, "result": "kept", "kernel_fresh": False}


def test_sync_loop_kills_the_kernel_and_reports_the_reset() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict, int | None]:
        await h.js("let lost = 1")
        process = h.kernel_process()
        looping = await h.js("while (true) {}", timeout_ms=200)
        await asyncio.wait_for(process.wait(), 5)
        after = await h.js("typeof lost")
        return looping, after, process.returncode

    scenario.options = {"kill_grace_seconds": 0.3}
    looping, after, returncode = _run(scenario)
    assert looping["ok"] is False
    assert looping["error"]["code"] == "js_kernel_reset"
    assert "all bindings were discarded" in looping["error"]["message"]
    assert returncode is not None
    assert after["kernel_fresh"] is True and after["result"] == "undefined"


def test_idle_kernel_is_reaped_and_the_next_call_is_fresh() -> None:
    async def scenario(h: _Harness) -> tuple[int | None, dict]:
        await h.js("let idle = 1")
        process = h.kernel_process()
        await asyncio.wait_for(process.wait(), 5)
        return process.returncode, await h.js("typeof idle")

    scenario.options = {"idle_seconds": 0.2}
    returncode, after = _run(scenario)
    assert returncode is not None
    assert after["kernel_fresh"] is True and after["result"] == "undefined"


def test_computer_use_off_kills_the_kernel_and_refuses_calls() -> None:
    async def scenario(h: _Harness) -> tuple[int | None, dict]:
        await h.js("1")
        process = h.kernel_process()
        h.status = "off"
        await asyncio.wait_for(process.wait(), 5)
        refused = await h.js("1")
        return process.returncode, refused

    scenario.options = {"status_poll_seconds": 0.05}
    returncode, refused = _run(scenario)
    assert returncode is not None
    assert refused["error"]["code"] == "computer_use_unavailable"


def test_leftover_timer_cannot_reach_the_controller() -> None:
    async def scenario(h: _Harness) -> tuple[dict, list]:
        await h.js(
            "globalThis.late = new Promise((resolve) => setTimeout(() =>"
            " computer.app({ bundleId: 'com.apple.mail' })"
            ".then(() => resolve('reached'), (error) => resolve(error.code)), 100));"
            # Without this the completion value is the promise, which the call awaits.
            " null"
        )
        # The timer fires while this later call is executing.
        later = await h.js("await new Promise((r) => setTimeout(r, 300)); await late")
        return later, h.driver.calls

    later, calls = _run(scenario)
    assert later["result"] == "js_call_inactive"
    assert calls == []


def test_turn_end_cancels_the_call_and_keeps_bindings() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict, dict]:
        turn = h.controller.session_turns.in_flight[SESSION]
        pending = asyncio.create_task(
            h.js("var survivor = 'kept'; await new Promise(() => {})", timeout_ms=10000)
        )
        await asyncio.sleep(0.5)
        turn.task.cancel()
        ended = await asyncio.wait_for(pending, 5)
        refused = await h.js("1")
        h.start_turn("turn-2")
        after = await h.js("survivor")
        return ended, refused, after

    ended, refused, after = _run(scenario)
    assert ended["error"]["code"] == "script_turn_ended"
    assert refused["error"]["code"] == "active_turn_required"
    assert after["result"] == "kept" and after["kernel_fresh"] is False


def test_concurrent_calls_in_one_session_are_serialized() -> None:
    async def scenario(h: _Harness) -> Any:
        await h.js("globalThis.order = []")
        first = asyncio.create_task(
            h.js("order.push('a1'); await new Promise((r) => setTimeout(r, 200)); order.push('a2')")
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(h.js("order.push('b')"))
        await asyncio.gather(first, second)
        return (await h.js("order"))["result"]

    assert _run(scenario) == ["a1", "a2", "b"]


def test_exceptions_and_unserializable_results_are_reported() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict]:
        thrown = await h.js("null.boom")
        cyclic = await h.js("const loop = {}; loop.self = loop; loop")
        return thrown, cyclic

    thrown, cyclic = _run(scenario)
    assert thrown["error"]["code"] == "js_exception"
    assert thrown["error"]["name"] == "TypeError"
    assert "stack" in thrown["error"]["params"]
    assert cyclic["ok"] is True and cyclic["result"] is None
    assert "Circular" in cyclic["result_text"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "The JavaScript call failed: The JavaScript kernel was reset"),
        ("zh", "JavaScript 调用失败：JavaScript 内核已重置"),
    ],
)
def test_cli_envelope_and_localized_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    language: str,
    expected: str,
) -> None:
    from vibe import cli

    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
    monkeypatch.setattr(cli, "_configured_cli_language", lambda: language)
    seen: list[dict] = []
    code_file = tmp_path / "step.js"
    code_file.write_text("1 + 1", encoding="utf-8")

    def succeed(**kwargs):
        seen.append(kwargs)
        return {
            "status_code": 200,
            "body": {"ok": True, "result": 2, "console": "", "screenshots": [], "kernel_fresh": True},
        }

    monkeypatch.setattr("vibe.internal_client.computer_js_eval_sync", succeed)
    args = cli.build_parser().parse_args(["computer", "js", "--code", f"@{code_file}"])
    assert cli.cmd_computer(args) == 0
    assert seen == [{"caller_session_id": "ses-cli", "code": "1 + 1", "timeout_ms": 30000}]
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "ok": True,
        "kind": "computer_js",
        "result": 2,
        "console": "",
        "screenshots": [],
        "kernel_fresh": True,
    }

    error = {
        "name": "KernelReset",
        "code": "js_kernel_reset",
        "message": "raw",
        "params": {"reason": "unsettled"},
        "step": "script",
        "last_completed_step": None,
        "screenshot_path": None,
        "ambiguous": True,
    }
    monkeypatch.setattr(
        "vibe.internal_client.computer_js_eval_sync",
        lambda **_kwargs: {
            "status_code": 200,
            "body": {"ok": False, "error": error, "console": "", "screenshots": [], "kernel_fresh": False},
        },
    )
    args = cli.build_parser().parse_args(["computer", "js", "--code", "while (true) {}"])
    with pytest.raises(cli.TaskCliError) as exc_info:
        cli.cmd_computer(args)
    assert str(exc_info.value).startswith(expected)
    assert exc_info.value.code == "js_kernel_reset"
    assert exc_info.value.details["error"] == error

    with pytest.raises(cli.TaskCliError) as exc_info:
        cli.cmd_computer(
            cli.build_parser().parse_args(["computer", "js", "--code", "1", "--timeout-ms", "300001"])
        )
    assert exc_info.value.code == "invalid_timeout"
