from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core import internal_server, session_turns
from core.computer_script_interactions import (
    MAX_INTERACTION_REQUEST_BYTES,
    MAX_MESSAGE_BYTES,
    ComputerScriptInteractionError,
    ComputerScriptInteractionManager,
)
from core.computer_script_sessions import ComputerScriptSessionManager
from core.services.dispatch import SOURCE_SCHEDULED
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
    def __init__(self, *bindings) -> None:
        self.bindings = {binding.token: binding for binding in bindings}

    def admit(self, token: str):
        if token not in self.bindings:
            raise AssertionError(f"unexpected token: {token}")
        return self.bindings[token]


class _FakeController:
    def __init__(self, language: str = "en") -> None:
        self.config = SimpleNamespace(language=language)
        self.messages: list[tuple[MessageContext, str, str]] = []
        # ``"none"`` mimics the dispatcher's failed IM send; ``"raise"`` an error.
        self.delivery = "ok"
        self.release: asyncio.Event | None = None

    async def emit_agent_message(
        self,
        context: MessageContext,
        message_type: str,
        text: str,
    ) -> str | None:
        if self.release is not None:
            await self.release.wait()
        if self.delivery == "raise":
            raise RuntimeError("adapter failed")
        if self.delivery == "none":
            return None
        self.messages.append((context, message_type, text))
        return f"message-{len(self.messages)}"


def _manager(
    *,
    platform: str = "avibe",
    owner_id: str = "owner",
) -> tuple[ComputerScriptInteractionManager, _FakeController, SimpleNamespace]:
    context = _context(user_id=owner_id, platform=platform)
    turn = SimpleNamespace(context=context)
    bindings = [
        SimpleNamespace(token=token, session_id="session", turn=turn)
        for token in ("script-token", "second-script-token")
    ]
    controller = _FakeController()
    manager = ComputerScriptInteractionManager(
        controller,
        _FakeScriptSessions(*bindings),
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


@pytest.mark.parametrize(
    ("operation", "delivery", "code"),
    [
        ("notify", "none", "computer_script_notify_failed"),
        ("notify", "raise", "computer_script_notify_failed"),
        ("confirm", "none", "confirm_prompt_failed"),
        ("confirm", "raise", "confirm_prompt_failed"),
    ],
)
def test_undelivered_interaction_fails_typed(
    operation: str, delivery: str, code: str
) -> None:
    """A message the owner never received is never reported as delivered."""

    async def exercise() -> tuple[str, bool]:
        manager, controller, _turn = _manager(platform="slack")
        controller.delivery = delivery
        try:
            await getattr(manager, operation)("script-token", "Send it?")
        except ComputerScriptInteractionError as exc:
            return exc.code, manager.pending_for_token("script-token") is None
        raise AssertionError("undelivered interaction succeeded")

    assert asyncio.run(exercise()) == (code, True)


def test_confirm_is_answerable_only_after_its_prompt_is_delivered() -> None:
    async def exercise() -> dict:
        manager, controller, _turn = _manager(platform="slack")
        controller.release = asyncio.Event()
        pending = asyncio.create_task(manager.confirm("script-token", "Send it?"))
        await asyncio.sleep(0)
        answer = {"session_id": "session", "approver_id": "owner", "message": "yes"}
        assert manager.pending_for_token("script-token") is not None
        assert manager.match_answer(**answer) is None
        controller.release.set()
        for _ in range(3):
            await asyncio.sleep(0)
        matched = manager.match_answer(**answer)
        assert matched is not None
        manager.resolve_answer(*matched)
        return await pending

    assert asyncio.run(exercise())["effect"] == "confirmed"


def test_confirm_from_an_unattended_turn_is_refused_before_any_prompt() -> None:
    """Nobody launched a scheduled Turn, so nobody is there to answer."""

    async def exercise() -> tuple[str, list]:
        manager, controller, turn = _manager(platform="slack")
        turn.source = SOURCE_SCHEDULED
        try:
            await manager.confirm("script-token", "Send it?")
        except ComputerScriptInteractionError as exc:
            return exc.code, controller.messages
        raise AssertionError("unattended confirm was admitted")

    assert asyncio.run(exercise()) == ("confirm_unattended", [])


def test_a_session_has_at_most_one_pending_confirm() -> None:
    """A yes/no reply names no prompt, so a second script cannot open another."""

    async def exercise() -> tuple[str, bool]:
        manager, _controller, _turn = _manager(platform="slack")
        first = asyncio.create_task(manager.confirm("script-token", "Send A?"))
        await asyncio.sleep(0)
        try:
            await manager.confirm("second-script-token", "Send B?")
        except ComputerScriptInteractionError as exc:
            code = exc.code
        else:
            raise AssertionError("second confirm was admitted")
        still_waiting = not first.done()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        return code, still_waiting

    assert asyncio.run(exercise()) == ("confirm_already_pending", True)


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


@pytest.mark.parametrize(
    ("ending", "code"),
    [("turn_ended", "script_turn_ended"), ("closed", "script_connection_invalid")],
)
def test_script_connection_end_wakes_a_pending_confirm(ending: str, code: str) -> None:
    """No confirm outlives its script, whether the Turn ends or the script closes."""

    async def exercise() -> tuple[str, bool]:
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
        if ending == "turn_ended":
            script_sessions.invalidate_turn(turn)
        else:
            script_sessions.close(token)
        try:
            await pending
        except ComputerScriptInteractionError as exc:
            return exc.code, interactions.match_answer(
                session_id="session", approver_id="owner", message="yes"
            ) is None
        finally:
            script_sessions.close_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("ended script left confirm pending")

    assert asyncio.run(exercise()) == (code, True)


def test_internal_routes_bound_interactions_and_reject_oversized_messages() -> None:
    async def exercise() -> tuple[httpx.Response, ...]:
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
                # The largest admitted message, nearly every byte JSON-escaped to six bytes.
                escaped_limit = await client.post(
                    "/internal/computer-script/notify",
                    json={
                        "connection_token": token,
                        "message": "x" + "\x01" * (MAX_MESSAGE_BYTES - 1),
                    },
                )
                oversized = await client.post(
                    "/internal/computer-script/notify",
                    content=b"x" * (MAX_INTERACTION_REQUEST_BYTES + 1),
                    headers={"Content-Type": "application/json"},
                )
            return notify, escaped_limit, oversized, messages
        finally:
            controller.computer_script_sessions.close_all()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    notify, escaped_limit, oversized, messages = asyncio.run(exercise())
    assert notify.status_code == 200
    assert notify.json()["ok"] is True
    assert notify.json()["result"]["route"] == "message_dispatcher"
    assert escaped_limit.status_code == 200
    assert escaped_limit.json()["ok"] is True
    assert oversized.status_code == 413
    assert oversized.json()["error"] == "computer_script_message_too_large"
    assert oversized.json()["params"] == {"limit_bytes": MAX_MESSAGE_BYTES}
    assert len(messages) == 2
