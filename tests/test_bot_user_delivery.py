"""MESSAGE-DELIVERY-339: the user's own message to a Bot is P1.

The Web composer (Web, phone, desktop pet) posts to one route. In a Bot's
conversation that route admits the user's message as P1, the released P1 path
takes it from there: an idle Bot starts at once, a working Bot has it steered
into the Turn it is on, and a definitive refusal leaves it waiting in the FIFO,
undelivered. An ordinary Session keeps the Web composer's P3.

Only the native steer is replaced. Storage, admission, the Web route, the
controller's dispatch, and the HTTP projections are real.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from core import internal_server
from core.services.agent_steering import SteerOutcome, result as steer_result
from storage import message_deliveries, messages_service
from storage.db import create_sqlite_engine
from tests.test_internal_server import _build_controller_double, _reserve_submission
from tests.test_ui_session_stream import _make_session, isolated_state  # noqa: F401
from core.bots import BotService
from tests.ui_server_test_helpers import csrf_headers

ORIGIN = "http://127.0.0.1:15131"


def _make_bot() -> str:
    from core.bots import BotService
    from core.vibe_agents import VibeAgentStore

    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents([])
    finally:
        store.close()
    with BotService() as service:
        return service.create_bot(display_name="Security Lead", handle="cso").session_id


def _start_active_turn(engine, session_id: str, *, backend: str) -> str:
    """A Turn the Session is working on, its native turn known."""

    turn_id = message_deliveries.new_turn_id()
    with engine.begin() as conn:
        initial = _reserve_submission(conn, scope_id=None, session_id=session_id, text="active work")
        message_deliveries.insert_turn(
            conn, turn_id=turn_id, session_id=session_id,
            initial_delivery_id=initial["id"], state="starting", backend=backend,
        )
        message_deliveries.open_start_attempt(
            conn, initial["id"], expected_version=initial["version"],
            turn_id=turn_id, attempt_id=message_deliveries.new_attempt_id(),
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        assert message_deliveries.bind_native_start(
            conn, turn_id, expected_version=turn["version"],
            runtime_key="runtime-key", runtime_turn_id="runtime-token", native_turn_id="native-turn",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test"})
    return turn_id


def _send(session_id: str, text: str, *, steer_outcome: SteerOutcome | None = None):
    """Post ``text`` from the Web composer; returns the response, the steers, and whether a Turn ran."""

    from vibe.ui_server import app

    started: list[str] = []

    async def handler(ctx, prompt):
        started.append(prompt)
        controller.mark_turn_complete(ctx)

    controller = _build_controller_double(handler=handler)
    internal_app = internal_server.create_app(controller)
    steers = []

    async def steer(_backend, request):
        steers.append(request)
        return steer_result(steer_outcome, reason="not_steerable" if steer_outcome == SteerOutcome.REFUSED else None)

    controller.session_turns._steer = steer

    async def dispatch(payload):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://controller.test",
        ) as client:
            response = await client.post("/internal/dispatch_async", json=payload)
            # An idle admission starts the Turn in the background: let it run.
            for _ in range(50):
                if started or steers:
                    break
                await asyncio.sleep(0.02)
        return {"status_code": response.status_code, "body": response.json()}

    client = app.test_client()
    headers = csrf_headers(client, ORIGIN)
    with patch("vibe.internal_client.dispatch_async", side_effect=dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages", json={"text": text}, headers=headers, base_url=ORIGIN,
        )
    return response, steers, started


@pytest.fixture()
def bot_events():
    """``bots.changed`` as the controller bus publishes it, in order."""
    from core.inbox_events import BOTS_CHANGED_EVENT, bus

    events: list[dict] = []
    sub_id = bus.subscribe_callback(
        lambda event_type, data: events.append(dict(data)) if event_type == BOTS_CHANGED_EVENT else None
    )
    yield events
    bus.unsubscribe(sub_id)


def _bot_order() -> list[str]:
    from core.bots import BotService

    with BotService() as service:
        return [bot.handle for bot in service.list_bots()]


def _delivery(engine, delivery_id: str) -> dict:
    with engine.connect() as conn:
        return message_deliveries.get_delivery(conn, delivery_id)


def _transcript(engine, session_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = messages_service.list_session_messages(conn, session_id=session_id, types=("user",))["messages"]
    return [row["text"] for row in rows]


def test_an_idle_bot_starts_on_the_users_message_at_once(isolated_state, bot_events):
    """MESSAGE-DELIVERY-339: an idle Bot starts a Turn on the user's message at once."""

    session_id = _make_bot()
    with BotService() as service:
        service.create_bot(display_name="Product", handle="pm")
    assert _bot_order() == ["main", "cso", "pm"]
    engine = create_sqlite_engine()
    bot_events.clear()

    response, steers, started = _send(session_id, "帮我看看这周的依赖告警")

    assert response.status_code in (201, 202), response.get_json()
    assert steers == []
    assert started and "帮我看看这周的依赖告警" in started[0]
    body = response.get_json()
    delivery = _delivery(engine, body.get("delivery_id") or body["id"])
    assert delivery["priority"] == "p1"
    assert "帮我看看这周的依赖告警" in _transcript(engine, session_id)
    # bots.md D13: the Bot the user just wrote to moves up, and every open list is told to read the order again.
    assert _bot_order() == ["main", "cso", "pm"]
    assert bot_events == [{"handle": "cso"}]

    bot_events.clear()
    _send("ses_pm", "排一下下周的需求")
    assert _bot_order() == ["main", "pm", "cso"]
    assert bot_events == [{"handle": "pm"}]


@pytest.mark.parametrize(
    ("outcome", "state", "delivered"),
    [(SteerOutcome.ACCEPTED, "accepted", True), (SteerOutcome.REFUSED, "queued", False)],
    ids=["steered", "refused"],
)
def test_a_working_bot_takes_the_users_message_into_its_turn(isolated_state, bot_events, outcome, state, delivered):
    """MESSAGE-DELIVERY-339: a working Bot has it steered in; a definitive refusal leaves it undelivered."""

    session_id = _make_bot()
    engine = create_sqlite_engine()
    turn_id = _start_active_turn(engine, session_id, backend="vibey")
    bot_events.clear()

    response, steers, started = _send(session_id, "先停一下，换成看 avault", steer_outcome=outcome)

    # One steer into the exact Turn the Bot is on; no second Turn, no Stop.
    assert [(request.expected_logical_turn_id, request.expected_native_turn_id, request.text) for request in steers] == [
        (turn_id, "native-turn", "先停一下，换成看 avault"),
    ]
    assert started == []
    body = response.get_json()
    delivery = _delivery(engine, body["id"])
    assert delivery["state"] == state
    if delivered:
        assert response.status_code == 201
        assert "先停一下，换成看 avault" in _transcript(engine, session_id)
        assert bot_events == [{"handle": "cso"}]
    else:
        # Refused: it waits for the Turn to end, and nothing claims it arrived, the Bot's place included (D13).
        assert bot_events == []
        assert (response.status_code, body["queued"]) == (202, True)
        assert delivery["priority"] == "p3"
        assert "先停一下，换成看 avault" not in _transcript(engine, session_id)
        with engine.connect() as conn:
            assert [row["id"] for row in message_deliveries.list_queued(conn, session_id)] == [body["id"]]


def test_an_ordinary_session_keeps_the_composers_queue(isolated_state, tmp_path):
    """MESSAGE-DELIVERY-339: only a Bot's conversation changes; an ordinary Session keeps P3."""

    _scope_id, session_id = _make_session(tmp_path, agent_backend="codex")
    engine = create_sqlite_engine()
    _start_active_turn(engine, session_id, backend="codex")

    response, steers, started = _send(session_id, "while busy", steer_outcome=SteerOutcome.ACCEPTED)

    assert (response.status_code, response.get_json()["queued"]) == (202, True)
    assert steers == [] and started == []
    delivery = _delivery(engine, response.get_json()["id"])
    assert (delivery["priority"], delivery["state"]) == ("p3", "queued")
