from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core import internal_server, session_turns
from core.computer_script_interactions import (
    MAX_INTERACTION_REQUEST_BYTES,
    MAX_MESSAGE_BYTES,
    NOTIFY_EVENT,
    ComputerScriptInteractionError,
    ComputerScriptInteractionManager,
)
from modules.im import MessageContext
from vibe.message_types import spec_for


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
        # The ``output`` each delivered message carried, aligned with ``messages``.
        self.outputs: list = []
        # ``"none"`` mimics the dispatcher's failed IM send; ``"raise"`` an error.
        self.delivery = "ok"

    async def emit_agent_message(
        self,
        context: MessageContext,
        message_type: str,
        text: str,
        *,
        output=None,
    ) -> str | None:
        if self.delivery == "raise":
            raise RuntimeError("adapter failed")
        if self.delivery == "none":
            return None
        self.messages.append((context, message_type, text))
        self.outputs.append(output)
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


def test_script_words_carry_the_event_the_web_draws_as_an_agent_message() -> None:
    """MESSAGE-DELIVERY-035: a script's notify message draws as an Agent bubble mid-Turn.

    It stays a ``notify`` row, so every IM surface delivers it unchanged; the
    event moves it out of the Web's compact status pill and into an Agent
    bubble, so it must be one the shared catalog declares for that.
    """

    async def exercise() -> _FakeController:
        manager, controller, _turn = _manager(platform="avibe")
        await manager.notify("script-token", "Calculator shows 42")
        return controller

    controller = asyncio.run(exercise())
    agent_render_events = spec_for("notify")["agentRenderWhenEvents"]
    observed = [
        (message_type, (output.metadata.get("event") if output is not None else None))
        for (_context, message_type, _text), output in zip(
            controller.messages, controller.outputs, strict=True
        )
    ]
    assert observed == [("notify", NOTIFY_EVENT)]
    assert controller.messages[0][2] == "Calculator shows 42"
    assert NOTIFY_EVENT in agent_render_events
    output = controller.outputs[0]
    # Metadata only: the row must not gain lifecycle authority over the Turn.
    assert not output.completes_turn
    assert not output.detached
    assert output.idempotency_key is None


@pytest.mark.parametrize("delivery", ["none", "raise"])
def test_undelivered_notify_fails_typed(delivery: str) -> None:
    """A message the owner never received is never reported as delivered."""

    async def exercise() -> str:
        manager, controller, _turn = _manager(platform="slack")
        controller.delivery = delivery
        try:
            await manager.notify("script-token", "Draft is ready")
        except ComputerScriptInteractionError as exc:
            return exc.code
        raise AssertionError("undelivered notify succeeded")

    assert asyncio.run(exercise()) == "computer_script_notify_failed"


def test_internal_routes_bound_notify_reject_oversized_and_omit_confirm() -> None:
    async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response, list[str]]:
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
