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
        self.delay = 0.0

    async def call_tool(self, name: str, arguments: dict, *, handle_descriptor=None) -> dict:
        del handle_descriptor
        self.calls.append((name, arguments))
        await asyncio.sleep(self.delay)
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


def test_top_level_let_and_const_redeclare_across_calls_in_any_order() -> None:
    async def scenario(h: _Harness) -> list[dict]:
        return [
            await h.js("const win = 1"),
            await h.js("let win = 2; win"),  # const -> let
            await h.js("let win = 3; win"),  # let -> let
            await h.js("const win = 4; win"),  # let -> const
            await h.js("const win = 5; win"),  # const -> const
            await h.js("const { a, b: [c] } = { a: 1, b: [2] }; a + c"),
            await h.js("let a = 10, c = 20; win = 6; a + c + win"),
        ]

    results = _run(scenario)
    assert all(result["ok"] is True for result in results), results
    assert [result["result"] for result in results] == [None, 2, 3, 4, 5, 3, 36]


def test_const_rewrite_leaves_text_property_names_and_loop_heads_unchanged() -> None:
    source = (
        'const text = "const x = 1" + `const ${"y"}` + /const z/.source; // const c\n'
        "const shape = { const: 1 }, methods = { const() { return 2 } };\n"
        "class Box { const = 3; static const() { return 4 } }\n"
        "const values = [];\n"
        "for (const item of [5, 6]) { values.push(item) }\n"
        "({ text, key: shape.const, method: methods.const(), field: new Box().const,"
        " statik: Box.const(), values })"
    )

    async def scenario(h: _Harness) -> list[dict]:
        return [await h.js(source), await h.js("let text = 'again'; text")]

    first, second = _run(scenario)
    assert first["ok"] is True, first
    assert first["result"] == {
        "text": "const x = 1const yconst z",
        "key": 1,
        "method": 2,
        "field": 3,
        "statik": 4,
        "values": [5, 6],
    }
    assert second["result"] == "again"


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


def test_leftover_timer_of_an_ended_call_never_fires() -> None:
    async def scenario(h: _Harness) -> tuple[dict, list]:
        await h.js(
            "globalThis.fired = false; setTimeout(() => { fired = true;"
            " computer.app({ bundleId: 'com.apple.mail' }) }, 100); null"
        )
        # The timer is due while this later call is executing.
        later = await h.js("await new Promise((r) => setTimeout(r, 300)); fired")
        return later, h.driver.calls

    later, calls = _run(scenario)
    assert later["result"] is False
    assert calls == []


def test_cancelled_continuation_cannot_overwrite_a_later_binding() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict]:
        cancelled = await h.js(
            "var x = 0; await new Promise((r) => setTimeout(r, 300)); x = 1",
            timeout_ms=100,
        )
        await h.js("x = 2")
        await asyncio.sleep(0.5)
        return cancelled, await h.js("x")

    cancelled, after = _run(scenario)
    assert cancelled["error"]["code"] == "js_timeout"
    assert after["result"] == 2 and after["kernel_fresh"] is False


def test_sdk_request_of_a_cancelled_call_never_settles() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict]:
        h.driver.delay = 0.4
        cancelled = await h.js(
            "globalThis.outcome = 'pending'; await computer.app({ bundleId: 'com.apple.mail' })"
            ".then(() => { outcome = 'resolved' }, (error) => { outcome = error.code })",
            timeout_ms=150,
        )
        await asyncio.sleep(0.8)
        return cancelled, await h.js("outcome")

    cancelled, after = _run(scenario)
    assert cancelled["error"]["code"] == "js_timeout"
    assert after["result"] == "pending"


def test_a_continuation_resumed_by_a_later_call_cannot_reach_the_controller() -> None:
    """Known-by-design residual: the model can wake a cancelled continuation
    itself, which may change bindings but never reaches the Controller."""

    async def scenario(h: _Harness) -> tuple[dict, list]:
        await h.js(
            "globalThis.after = 'waiting'; await new Promise((r) => { globalThis.release = r });"
            " after = 'resumed'; await computer.app({ bundleId: 'com.apple.mail' })",
            timeout_ms=150,
        )
        later = await h.js("release(); await new Promise((r) => setTimeout(r, 200)); after")
        return later, h.driver.calls

    later, calls = _run(scenario)
    assert later["result"] == "resumed"
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


def test_a_queued_call_stays_bound_to_its_turn() -> None:
    async def scenario(h: _Harness) -> tuple[dict, dict, dict, list]:
        await h.js("1")
        turn_a = h.controller.session_turns.in_flight[SESSION]
        running = asyncio.create_task(h.js("await new Promise((r) => setTimeout(r, 500))"))
        await asyncio.sleep(0.2)
        queued = asyncio.create_task(
            h.js("globalThis.executed = true; await computer.app({ bundleId: 'com.apple.mail' })")
        )
        await asyncio.sleep(0.1)
        # Turn B is live before the queued Turn-A call can take the lock.
        turn_a.task.cancel()
        h.start_turn("turn-b")
        first, second = await asyncio.wait_for(asyncio.gather(running, queued), 5)
        after = await h.js("typeof executed")
        return first, second, after, h.driver.calls

    first, second, after, calls = _run(scenario)
    assert first["error"]["code"] == "script_turn_ended"
    assert second["error"]["code"] == "script_turn_ended"
    assert after["result"] == "undefined" and after["kernel_fresh"] is False
    assert calls == []


def test_computer_use_off_cancels_an_executing_call() -> None:
    async def scenario(h: _Harness) -> tuple[dict, int | None]:
        await h.js("1")
        process = h.kernel_process()
        running = asyncio.create_task(h.js("await new Promise(() => {})", timeout_ms=10000))
        await asyncio.sleep(0.2)
        h.status = "off"
        report = await asyncio.wait_for(running, 5)
        await asyncio.wait_for(process.wait(), 5)
        return report, process.returncode

    scenario.options = {"status_poll_seconds": 0.05}
    report, returncode = _run(scenario)
    assert report["error"]["code"] == "js_kernel_reset"
    assert report["error"]["params"] == {"reason": "computer_use_off"}
    assert returncode is not None


async def _reset_event(h: _Harness) -> None:
    await h.reset()


async def _archive_event(h: _Harness) -> None:
    h.kernels.discard_session(SESSION)


async def _off_event(h: _Harness) -> None:
    h.status = "off"


async def _shutdown_event(h: _Harness) -> None:
    h.kernels.close_all()


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (_reset_event, "reset"),
        (_archive_event, "session_archived"),
        (_off_event, "computer_use_off"),
        (_shutdown_event, "shutdown"),
    ],
)
def test_invalidation_cancels_every_admitted_call(event, reason: str) -> None:
    """Invariant: calls admitted before an invalidation never execute."""

    async def scenario(h: _Harness) -> tuple[list[dict], list, Any]:
        await h.js("1")
        executing = asyncio.create_task(h.js("await new Promise(() => {})", timeout_ms=10000))
        await asyncio.sleep(0.2)
        queued = [
            asyncio.create_task(
                h.js("globalThis.executed = true; await computer.app({ bundleId: 'com.apple.mail' })")
            )
            for _ in range(3)
        ]
        await asyncio.sleep(0.1)
        await event(h)
        reports = await asyncio.wait_for(asyncio.gather(executing, *queued), 5)
        await asyncio.sleep(0.2)
        return reports, h.driver.calls, h.kernels._slots.get(SESSION)

    scenario.options = {"status_poll_seconds": 0.05}
    reports, calls, slot = _run(scenario)
    for report in reports:
        assert report["error"]["code"] == "js_kernel_reset"
        assert report["error"]["params"] == {"reason": reason}
    assert calls == []
    assert slot is None


def test_a_queued_call_keeps_its_full_budget_after_taking_the_lock() -> None:
    async def scenario(h: _Harness) -> dict:
        await h.js("1")
        first = asyncio.create_task(h.js("await new Promise((r) => setTimeout(r, 600))"))
        await asyncio.sleep(0.1)
        second = await h.js(
            "await new Promise((r) => setTimeout(r, 300)); 'finished'",
            timeout_ms=400,
        )
        await first
        return second

    assert _run(scenario)["result"] == "finished"


def test_the_session_queue_is_capped() -> None:
    async def scenario(h: _Harness) -> dict:
        await h.js("1")
        executing = asyncio.create_task(h.js("await new Promise((r) => setTimeout(r, 400))"))
        await asyncio.sleep(0.1)
        queued = asyncio.create_task(h.js("1"))
        await asyncio.sleep(0.05)
        refused = await h.js("1")
        await asyncio.gather(executing, queued)
        return refused

    scenario.options = {"queue_limit": 1}
    assert _run(scenario)["error"]["code"] == "js_kernel_busy"


def test_non_posix_platform_is_refused_before_any_process_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import computer_js_kernels

    async def no_process(*_args, **_kwargs):
        raise AssertionError("a kernel process was started")

    monkeypatch.setattr(computer_js_kernels, "_kernel_platform_supported", lambda: False)
    monkeypatch.setattr(computer_js_kernels.asyncio, "create_subprocess_exec", no_process)

    async def scenario(h: _Harness) -> dict:
        return await h.js("1")

    report = _run(scenario)
    assert report["error"]["code"] == "platform_unsupported"
    assert report["kernel_fresh"] is False


def test_the_cli_never_stops_waiting_before_the_controller_lease_ends() -> None:
    from core.computer_cli import COMPUTER_JS_CONNECTION_LEASE_SECONDS
    from core.computer_js_kernels import (
        KILL_GRACE_SECONDS,
        MAX_TIMEOUT_MS,
        QUEUE_LIMIT,
        SPAWN_TIMEOUT_SECONDS,
    )
    from vibe.internal_client import computer_js_client_timeout_seconds

    assert computer_js_client_timeout_seconds() >= COMPUTER_JS_CONNECTION_LEASE_SECONDS
    # Every queued predecessor plus the call itself settles inside the lease.
    per_call = MAX_TIMEOUT_MS / 1000 + KILL_GRACE_SECONDS + SPAWN_TIMEOUT_SECONDS
    assert (QUEUE_LIMIT + 1) * per_call <= COMPUTER_JS_CONNECTION_LEASE_SECONDS


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


def test_cli_envelope_and_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
) -> None:
    from vibe import cli

    expected = "The JavaScript call failed: The JavaScript kernel was reset"
    monkeypatch.setenv("AVIBE_SESSION_ID", "ses-cli")
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
