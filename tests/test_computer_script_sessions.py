from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from core import internal_server, session_turns
from core.computer_script_sessions import (
    ComputerScriptSessionError,
    ComputerScriptSessionManager,
)
from modules.im import MessageContext


def _structured(payload: dict, *, is_error: bool = False) -> dict:
    return {
        "isError": is_error,
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }


def _context(turn_id: str) -> MessageContext:
    return MessageContext(
        user_id="owner",
        channel_id="channel",
        platform="avibe",
        platform_specific={
            "agent_session_id": "ses-script",
            "turn_token": turn_id,
        },
    )


def test_script_manager_binds_to_the_exact_turn_and_retains_terminal_code() -> None:
    async def exercise() -> tuple[str, str]:
        controller = SimpleNamespace(
            _session_id_from_context=lambda context: context.platform_specific[
                "agent_session_id"
            ],
        )
        turn_manager = session_turns.SessionTurnManager(controller)
        first_task = asyncio.create_task(asyncio.sleep(60))
        first_turn = session_turns.Turn(
            task=first_task,
            context=_context("turn-1"),
            logical_turn_id="turn-1",
        )
        turn_manager.in_flight["ses-script"] = first_turn
        manager = ComputerScriptSessionManager(turn_manager)
        opened = manager.open("ses-script", 30)
        token = opened["connection_token"]

        second_task = asyncio.create_task(asyncio.sleep(60))
        turn_manager.in_flight["ses-script"] = session_turns.Turn(
            task=second_task,
            context=_context("turn-2"),
            logical_turn_id="turn-2",
        )
        try:
            try:
                manager.admit(token)
            except ComputerScriptSessionError as exc:
                first_code = exc.code
            try:
                manager.admit(token)
            except ComputerScriptSessionError as exc:
                second_code = exc.code
            return first_code, second_code
        finally:
            manager.close_all()
            first_task.cancel()
            second_task.cancel()
            for task in (first_task, second_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    assert asyncio.run(exercise()) == ("script_turn_ended", "script_turn_ended")


def test_script_manager_invalidates_when_the_bound_turn_finishes() -> None:
    async def exercise() -> str:
        controller = SimpleNamespace(
            _session_id_from_context=lambda context: context.platform_specific[
                "agent_session_id"
            ],
        )
        turn_manager = session_turns.SessionTurnManager(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        turn_manager.in_flight["ses-script"] = session_turns.Turn(
            task=task,
            context=_context("turn-1"),
            logical_turn_id="turn-1",
        )
        manager = ComputerScriptSessionManager(turn_manager)
        token = manager.open("ses-script", 30)["connection_token"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        try:
            manager.admit(token)
        except ComputerScriptSessionError as exc:
            return exc.code
        finally:
            manager.close_all()
        raise AssertionError("finished Turn was admitted")

    assert asyncio.run(exercise()) == "script_turn_ended"


def test_script_manager_enforces_one_total_deadline() -> None:
    async def exercise() -> str:
        controller = SimpleNamespace(
            _session_id_from_context=lambda context: context.platform_specific[
                "agent_session_id"
            ],
        )
        turn_manager = session_turns.SessionTurnManager(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        turn_manager.in_flight["ses-script"] = session_turns.Turn(
            task=task,
            context=_context("turn-1"),
            logical_turn_id="turn-1",
        )
        manager = ComputerScriptSessionManager(turn_manager)
        token = manager.open("ses-script", 0.01)["connection_token"]
        await asyncio.sleep(0.02)
        try:
            manager.admit(token)
        except ComputerScriptSessionError as exc:
            return exc.code
        finally:
            manager.close_all()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise AssertionError("expired script was admitted")

    assert asyncio.run(exercise()) == "script_timeout"


def test_script_manager_keeps_the_cli_allowlist_and_session_binding() -> None:
    async def exercise() -> tuple[str, str]:
        controller = SimpleNamespace(
            _session_id_from_context=lambda context: context.platform_specific[
                "agent_session_id"
            ],
        )
        turn_manager = session_turns.SessionTurnManager(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        turn_manager.in_flight["ses-script"] = session_turns.Turn(
            task=task,
            context=_context("turn-1"),
            logical_turn_id="turn-1",
        )
        manager = ComputerScriptSessionManager(turn_manager)
        token = manager.open("ses-script", 30)["connection_token"]
        try:
            try:
                manager.prepare_tool_request(token, "start_session", {})
            except ComputerScriptSessionError as exc:
                tool_code = exc.code
            try:
                manager.prepare_tool_request(
                    token,
                    "list_apps",
                    {"session": "other-session"},
                )
            except ComputerScriptSessionError as exc:
                session_code = exc.code
            return tool_code, session_code
        finally:
            manager.close_all()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert asyncio.run(exercise()) == (
        "invalid_computer_script_tool",
        "session_argument_forbidden",
    )


def test_script_routes_reject_an_old_turn_before_reaching_computer_server() -> None:
    async def exercise() -> tuple[dict, dict, list[tuple[str, dict]]]:
        calls: list[tuple[str, dict]] = []

        class ComputerServer:
            async def call_tool(self, name: str, arguments: dict) -> dict:
                calls.append((name, arguments))
                return _structured({"windows": []})

        controller = SimpleNamespace(
            computer_use_server=ComputerServer(),
            computer_script_sessions=None,
            controller_id="controller-test",
            _session_id_from_context=lambda context: context.platform_specific[
                "agent_session_id"
            ],
        )
        app = internal_server.create_app(controller)
        first_task = asyncio.create_task(asyncio.sleep(60))
        controller.session_turns.in_flight["ses-script"] = session_turns.Turn(
            task=first_task,
            context=_context("turn-1"),
            logical_turn_id="turn-1",
        )
        transport = httpx.ASGITransport(app=app)
        second_task = asyncio.create_task(asyncio.sleep(60))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                opened = await client.post(
                    "/internal/computer-script/open",
                    json={
                        "caller_session_id": "ses-script",
                        "timeout_seconds": 30,
                    },
                )
                token = opened.json()["connection_token"]
                success = await client.post(
                    "/internal/computer-script/request",
                    json={
                        "connection_token": token,
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
                controller.session_turns.in_flight["ses-script"] = session_turns.Turn(
                    task=second_task,
                    context=_context("turn-2"),
                    logical_turn_id="turn-2",
                )
                stale = await client.post(
                    "/internal/computer-script/request",
                    json={
                        "connection_token": token,
                        "tool": "list_windows",
                        "arguments": {},
                    },
                )
            return success.json(), stale.json(), calls
        finally:
            controller.computer_script_sessions.close_all()
            first_task.cancel()
            second_task.cancel()
            for task in (first_task, second_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    success, stale, calls = asyncio.run(exercise())
    assert success["ok"] is True
    assert stale["error"] == "script_turn_ended"
    assert calls == [("list_windows", {"session": "ses-script"})]
