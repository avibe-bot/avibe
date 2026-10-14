from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core import internal_server, session_turns
from core.computer_script_interactions import (
    ComputerScriptInteractionError,
    ComputerScriptInteractionManager,
)
from core.computer_script_sessions import ComputerScriptSessionManager
from modules.im import MessageContext


def _context(
    *,
    user_id: str = "owner",
    platform: str = "avibe",
    channel_id: str = "channel",
    thread_id: str | None = "thread",
) -> MessageContext:
    return MessageContext(
        user_id=user_id,
        channel_id=channel_id,
        platform=platform,
        thread_id=thread_id,
        platform_specific={},
    )


class _FakeScriptSessions:
    def __init__(self, binding) -> None:
        self.binding = binding

    def admit(self, token: str):
        if token != self.binding.token:
            raise AssertionError(f"unexpected token: {token}")
        return self.binding


class _FakeController:
    def __init__(self, language: str = "en") -> None:
        self.config = SimpleNamespace(language=language)
        self.messages: list[tuple[MessageContext, str, str]] = []

    def _t(self, key: str, **kwargs) -> str:
        if key == "computer.confirm.prompt":
            return (
                f"{kwargs['message']}\n\nReply yes or no to continue. "
                "Only the owner who started this script can answer."
            )
        if key == "computer.confirm.ownerOnly":
            return "Only the owner who started this script can answer this confirmation."
        if key == "computer.confirm.invalidAnswer":
            return "Please answer yes or no. The confirmation is still waiting."
        raise AssertionError(f"unexpected translation key: {key}")

    async def emit_agent_message(
        self,
        context: MessageContext,
        message_type: str,
        text: str,
    ) -> str:
        self.messages.append((context, message_type, text))
        return f"message-{len(self.messages)}"


def _manager(
    *,
    platform: str = "avibe",
    owner_id: str = "owner",
) -> tuple[ComputerScriptInteractionManager, _FakeController, SimpleNamespace]:
    context = _context(user_id=owner_id, platform=platform)
    turn = SimpleNamespace(context=context)
    binding = SimpleNamespace(token="script-token", session_id="session", turn=turn)
    controller = _FakeController()
    manager = ComputerScriptInteractionManager(
        controller,
        _FakeScriptSessions(binding),
    )
    return manager, controller, turn


@pytest.mark.parametrize("platform", ["avibe", "slack"])
def test_notify_reuses_the_launching_surface(platform: str) -> None:
    async def exercise() -> dict:
        manager, controller, _turn = _manager(platform=platform)
        result = await manager.notify("script-token", "A progress update")
        return {
            "result": result,
            "message": controller.messages[0],
        }

    observed = asyncio.run(exercise())
    assert observed["result"] == {
        "status": "confirmed",
        "effect": "confirmed",
        "route": "message_dispatcher",
        "delivery": {
            "mode": "foreground",
            "delivered_count": 1,
        },
        "message_id": "message-1",
    }
    context, message_type, text = observed["message"]
    assert context.platform == platform
    assert message_type == "notify"
    assert text == "A progress update"


def test_confirm_accepts_only_the_launching_owner_in_a_shared_surface() -> None:
    async def exercise() -> tuple[dict, list[tuple[MessageContext, str, str]]]:
        manager, controller, _turn = _manager(platform="slack")
        pending = asyncio.create_task(manager.confirm("script-token", "Send it?"))
        await asyncio.sleep(0)
        pending_question = manager.pending_for_token("script-token")
        assert pending_question is not None
        assert pending_question.expected_approver_id == "owner"

        member_context = _context(user_id="other-member", platform="slack")
        assert await manager.consume_answer(member_context, "no") is True
        assert pending.done() is False

        owner_context = _context(user_id="owner", platform="slack")
        assert await manager.consume_answer(owner_context, "yes") is True
        return await pending, controller.messages

    result, messages = asyncio.run(exercise())
    assert result["effect"] == "confirmed"
    assert messages[0][0].user_id == "owner"
    assert messages[0][1] == "notify"
    assert "owner" in messages[0][2].lower()
    assert messages[1][0].user_id == "other-member"
    assert "owner" in messages[1][2].lower()


def test_confirm_keeps_waiting_after_an_invalid_owner_answer() -> None:
    async def exercise() -> tuple[dict, list[tuple[MessageContext, str, str]]]:
        manager, controller, _turn = _manager()
        pending = asyncio.create_task(manager.confirm("script-token", "Continue?"))
        await asyncio.sleep(0)
        assert await manager.consume_answer(_context(), "maybe") is True
        assert pending.done() is False
        assert await manager.consume_answer(_context(), "yes") is True
        return await pending, controller.messages

    result, messages = asyncio.run(exercise())
    assert result["status"] == "confirmed"
    assert "yes or no" in messages[1][2].lower()


def test_confirm_refusal_is_typed_and_never_ambiguous() -> None:
    async def exercise() -> tuple[str, bool]:
        manager, _controller, _turn = _manager()
        pending = asyncio.create_task(manager.confirm("script-token", "Delete it?"))
        await asyncio.sleep(0)
        assert await manager.consume_answer(_context(), "no") is True
        try:
            await pending
        except ComputerScriptInteractionError as exc:
            return exc.code, False
        raise AssertionError("refused confirmation succeeded")

    code, ambiguous = asyncio.run(exercise())
    assert code == "confirm_refused"
    assert ambiguous is False


@pytest.mark.parametrize("invalidation_code", ["script_turn_ended", "script_timeout"])
def test_pending_confirm_is_invalidated_with_the_script_connection(
    invalidation_code: str,
) -> None:
    async def exercise() -> str:
        manager, _controller, _turn = _manager()
        pending = asyncio.create_task(manager.confirm("script-token", "Continue?"))
        await asyncio.sleep(0)
        manager.invalidate_token("script-token", invalidation_code)
        try:
            await pending
        except ComputerScriptInteractionError as exc:
            return exc.code
        raise AssertionError("invalidated confirmation remained pending")

    assert asyncio.run(exercise()) == invalidation_code


def test_turn_binding_invalidation_wakes_a_pending_confirm() -> None:
    async def exercise() -> str:
        controller = _FakeController()
        controller._session_id_from_context = lambda context: (
            context.platform_specific or {}
        ).get("agent_session_id")
        turn_manager = session_turns.SessionTurnManager(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        context = _context()
        context.platform_specific = {"agent_session_id": "session"}
        turn = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id="turn-1",
        )
        turn_manager.in_flight["session"] = turn
        script_sessions = ComputerScriptSessionManager(turn_manager)
        interactions = ComputerScriptInteractionManager(
            controller,
            script_sessions,
        )
        script_sessions.add_invalidation_listener(interactions.invalidate_token)
        token = script_sessions.open("session", 30)["connection_token"]
        pending = asyncio.create_task(interactions.confirm(token, "Continue?"))
        await asyncio.sleep(0)
        script_sessions.invalidate_turn(turn)
        try:
            await pending
        except ComputerScriptInteractionError as exc:
            return exc.code
        finally:
            script_sessions.close_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("Turn invalidation left confirm pending")

    assert asyncio.run(exercise()) == "script_turn_ended"


def test_internal_routes_bound_interactions_and_reject_oversized_messages() -> None:
    async def exercise() -> tuple[httpx.Response, httpx.Response, list[str]]:
        from tests.test_internal_server import _build_controller_double

        controller = _build_controller_double()
        messages: list[str] = []

        async def emit_agent_message(_context, _message_type, text, **_kwargs):
            messages.append(text)
            return f"message-{len(messages)}"

        controller.emit_agent_message = emit_agent_message
        app = internal_server.create_app(controller)
        task = asyncio.create_task(asyncio.sleep(60))
        context = _context(
            user_id="owner",
            platform="avibe",
        )
        context.platform_specific = {"agent_session_id": "ses-script"}
        turn = session_turns.Turn(
            task=task,
            context=context,
            logical_turn_id="turn-1",
        )
        controller.session_turns.in_flight["ses-script"] = turn
        token = controller.computer_script_sessions.open(
            "ses-script",
            30,
        )["connection_token"]
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                notify = await client.post(
                    "/internal/computer-script/notify",
                    json={
                        "connection_token": token,
                        "message": "Progress",
                    },
                )
                oversized = await client.post(
                    "/internal/computer-script/notify",
                    content=b"x" * (64 * 1024 + 1),
                    headers={"Content-Type": "application/json"},
                )
            return notify, oversized, messages
        finally:
            controller.computer_script_sessions.close_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    notify, oversized, messages = asyncio.run(exercise())
    assert notify.status_code == 200
    assert notify.json()["ok"] is True
    assert notify.json()["result"]["route"] == "message_dispatcher"
    assert oversized.status_code == 413
    assert oversized.json()["error"] == "computer_script_message_too_large"
    assert messages == ["Progress"]
