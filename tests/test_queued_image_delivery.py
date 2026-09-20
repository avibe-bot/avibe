"""Upload -> real busy admission -> queue -> native steer -> transcript.

Only the local controller transport and native app-server write are replaced.
All storage, attachment resolution, admission and HTTP projections are real.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from core import internal_server
from core.services.agent_steering import SteerOutcome, result as steer_result
from modules.agents.codex.agent import CodexAgent
from storage import message_deliveries, messages_service
from storage.db import create_sqlite_engine
from tests.test_agent_steering import (
    _CodexSessionManager,
    _CodexTransport,
    _CodexTurnRegistry,
    _primary_request,
)
from tests.test_internal_server import _build_controller_double, _reserve_submission
from tests.test_ui_session_stream import _make_session, isolated_state  # noqa: F401
from tests.ui_server_test_helpers import csrf_headers

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_queue_and_bootstrap_project_all_unaccepted_steering_states(isolated_state, tmp_path):
    """The wider UI read must not change claimable queue semantics or ordering."""
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path)
    states = ["queued", "pending_steer", "steering", "reconciling_steer", "reserved", "retired"]
    attachment = {"token": "projection-image", "name": "队列图片.png", "mime": "image/png"}
    turn_id = message_deliveries.new_turn_id()
    with create_sqlite_engine().begin() as conn:
        # Reverse insertion with tied timestamps tests the ID tiebreak, too.
        for index, state in reversed(list(enumerate(states))):
            steering = state in {"pending_steer", "steering", "reconciling_steer"}
            message_deliveries.insert_delivery(
                conn, delivery_id=f"msg_projection_{index}", session_id=session_id,
                priority="p3" if state == "queued" else "p1", state=state,
                snapshot=message_deliveries.message_snapshot(
                    scope_id=scope_id, session_id=session_id, platform="avibe",
                    author="user", source="user", content={"attachments": [attachment]},
                ),
                dispatch_text="", now="2026-09-20T00:00:00Z",
                current_attempt_id=message_deliveries.new_attempt_id() if steering else None,
                current_attempt_kind="steer" if steering else None,
                current_target_turn_id=turn_id if steering else None,
                current_expected_native_turn_id="native-turn" if state in {"steering", "reconciling_steer"} else None,
                current_receipt_outcome="unknown" if state == "reconciling_steer" else None,
            )
        assert [row["state"] for row in message_deliveries.list_queued(conn, session_id)] == ["queued"]
        initial = _reserve_submission(conn, scope_id=scope_id, session_id=session_id, text="claimed input")
        message_deliveries.insert_turn(
            conn, turn_id=turn_id, session_id=session_id,
            initial_delivery_id=initial["id"], state="starting", backend="claude",
        )
        assert message_deliveries.open_start_attempt(
            conn, initial["id"], expected_version=initial["version"],
            turn_id=turn_id, attempt_id=message_deliveries.new_attempt_id(),
        ) is not None

    client = app.test_client()
    queued = client.get(f"/api/sessions/{session_id}/queue").get_json()["queued"]
    with patch("vibe.api.get_vibe_agents", return_value={"agents": [], "default_agent_name": None}):
        bootstrap = client.get(f"/api/sessions/{session_id}/bootstrap").get_json()["queued"]
    assert queued == bootstrap
    assert [row["state"] for row in queued] == states[:4]
    assert [row["id"] for row in queued] == [f"msg_projection_{index}" for index in range(4)]
    assert all(row["content"]["attachments"] == [attachment] for row in queued)


@pytest.mark.parametrize("text", ["", "看看这张图片，不要丢掉附件"])
@pytest.mark.parametrize("receipt", ["accepted", "unknown_accepted", "unknown_refused", "refused"])
def test_uploaded_image_survives_real_queue_admission_and_send_now(isolated_state, tmp_path, text, receipt):
    """QUEUE-IMAGE-001/003/004: no disconnected producer/consumer fixtures."""
    from vibe.ui_server import app

    scope_id, session_id = _make_session(tmp_path, agent_backend="codex")
    engine = create_sqlite_engine()
    turn_id = message_deliveries.new_turn_id()
    with engine.begin() as conn:
        initial = _reserve_submission(conn, scope_id=scope_id, session_id=session_id, text="active work")
        message_deliveries.insert_turn(
            conn, turn_id=turn_id, session_id=session_id,
            initial_delivery_id=initial["id"], state="starting", backend="codex",
        )
        message_deliveries.open_start_attempt(
            conn, initial["id"], expected_version=initial["version"],
            turn_id=turn_id, attempt_id=message_deliveries.new_attempt_id(),
        )
        turn = message_deliveries.get_turn(conn, turn_id)
        assert message_deliveries.bind_native_start(
            conn, turn_id, expected_version=turn["version"],
            runtime_key="runtime-key", runtime_turn_id="runtime-token", native_turn_id="codex-turn",
        ) is not None
        assert message_deliveries.materialize_start_acceptance(conn, turn_id=turn_id, evidence={"kind": "test"})

    primary = _primary_request(session_id=session_id, backend="codex")
    primary.context.platform_specific["turn_token"] = turn_id
    native = _CodexTransport(error={
        "accepted": None,
        "unknown_accepted": TimeoutError("native write acknowledgement lost"),
        "unknown_refused": TimeoutError("native write acknowledgement lost"),
        "refused": RuntimeError("activeTurnNotSteerable"),
    }[receipt])
    agent = object.__new__(CodexAgent)
    agent.config = SimpleNamespace(include_time_info=False, include_user_info=False)
    agent._turn_registry = _CodexTurnRegistry(session_id, "codex-turn")
    agent._session_mgr = _CodexSessionManager(session_id, "codex-thread", primary.working_path)
    agent._transports = {primary.working_path: native}
    controller = _build_controller_double()
    controller.config.memory.enabled = False
    controller.agent_service = SimpleNamespace(
        agents={"codex": agent},
        _turn_gates={"runtime-key": SimpleNamespace(
            backend="codex", token="runtime-token", runtime_started=True,
            request=primary, context=primary.context, agent=agent,
        )},
    )
    agent.controller = controller
    internal_app = internal_server.create_app(controller)

    async def call_internal(path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app), base_url="http://controller.test",
        ) as client:
            response = await client.post(path, **kwargs)
        return {"status_code": response.status_code, "body": response.json()}

    async def dispatch(payload):
        return await call_internal("/internal/dispatch_async", json=payload)

    async def send_now(sid, *, expected_delivery_id):
        return await call_internal(
            f"/internal/send-now/{sid}", params={"expected_delivery_id": expected_delivery_id},
        )

    client = app.test_client()
    headers = csrf_headers(client, "http://127.0.0.1:15131")
    upload = client.post(
        f"/api/sessions/{session_id}/attachments",
        data={"upload_id": "queued-image-integration"},
        files={"file": ("队列图片.png", PNG, "image/png")},
        headers=headers, base_url="http://127.0.0.1:15131",
    )
    assert upload.status_code == 201
    attachment = upload.get_json()
    assert attachment["name"] == "队列图片.png"
    assert (attachment["width"], attachment["height"]) == (1, 1)
    with patch("vibe.internal_client.dispatch_async", side_effect=dispatch):
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": {"text": text, "attachments": [attachment]}},
            headers=headers, base_url="http://127.0.0.1:15131",
        )
    assert response.status_code == 202
    assert response.get_json()["queued"] is True
    delivery_id = response.get_json()["id"]
    queued = client.get(f"/api/sessions/{session_id}/queue").get_json()["queued"]
    assert len(queued) == 1
    assert queued[0]["id"] == delivery_id
    assert queued[0]["content"]["attachments"] == [attachment]
    with patch("vibe.api.get_vibe_agents", return_value={"agents": [], "default_agent_name": None}):
        bootstrap = client.get(f"/api/sessions/{session_id}/bootstrap").get_json()
    assert bootstrap["queued"][0]["content"]["attachments"] == [attachment]
    assert client.get(attachment["url"]).content == PNG

    def queue_projection():
        return client.get(f"/api/sessions/{session_id}/queue").get_json()["queued"]

    def bootstrap_projection():
        with patch("vibe.api.get_vibe_agents", return_value={"agents": [], "default_agent_name": None}):
            return client.get(f"/api/sessions/{session_id}/bootstrap").get_json()["queued"]

    committed_states = []

    def observe_queue_event(event, payload):
        if event == "queue.updated" and payload["session_id"] == session_id:
            # A separate connection proves the notification follows commit.
            with engine.connect() as conn:
                committed_states.append(message_deliveries.get_delivery(conn, delivery_id)["state"])

    native_send = native.send_request

    async def observe_native_write(method, params):
        # Read the projection while the native call is in flight without nesting
        # a synchronous test-client request in the request's own event loop.
        with engine.connect() as conn:
            rows = message_deliveries.list_queued(conn, session_id, include_steering=True)
        assert [(row["id"], row["state"]) for row in rows] == [(delivery_id, "steering")]
        assert rows[0]["content"]["attachments"] == [attachment]
        return await native_send(method, params)

    with (
        patch("vibe.internal_client.send_now", side_effect=send_now),
        patch("core.inbox_events.bus.publish", side_effect=observe_queue_event),
        patch.object(native, "send_request", side_effect=observe_native_write),
    ):
        sent = client.post(
            f"/api/sessions/{session_id}/queue/{delivery_id}/send-now",
            headers=headers, base_url="http://127.0.0.1:15131",
        )
    assert sent.status_code == 200
    assert sent.get_json()["status"] == {
        "accepted": "accepted", "unknown_accepted": "reconciling_steer",
        "unknown_refused": "reconciling_steer", "refused": "queued",
    }[receipt]
    assert committed_states == ["steering", sent.get_json()["status"]]
    assert len(native.calls) == 1
    method, params = native.calls[0]
    assert method == "turn/steer"
    assert params["expectedTurnId"] == "codex-turn"
    native_image = next(item for item in params["input"] if item["type"] == "localImage")
    assert Path(native_image["path"]).read_bytes() == PNG
    native_text = [item["text"] for item in params["input"] if item["type"] == "text"]
    assert native_text == ([text] if text else [])
    controller.command_handler.handle_stop.assert_not_awaited()
    controller.message_handler.handle_user_message.assert_not_awaited()

    with engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, delivery_id)
        message = messages_service.get_message(conn, delivery_id, session_id=session_id)
        active = message_deliveries.active_turn(conn, session_id)
    assert active["id"] == turn_id
    if receipt == "accepted":
        # Acceptance transfers the snapshot to the transcript and scrubs the
        # duplicate Delivery payload; it does not discard the attachment.
        assert delivery["snapshot_json"] is None
        assert message["content"]["attachments"] == [attachment]
        assert client.get(f"/api/sessions/{session_id}/queue").get_json()["queued"] == []
    else:
        assert message_deliveries.delivery_payload(delivery)["content"]["attachments"] == [attachment]
        assert message is None
        assert delivery["state"] == sent.get_json()["status"]
        for rows in (queue_projection(), bootstrap_projection()):
            assert [(row["id"], row["state"]) for row in rows] == [(delivery_id, delivery["state"])]
            assert rows[0]["content"]["attachments"] == [attachment]
        if receipt == "refused":
            assert sent.get_json()["reason"] == "native_turn_not_steerable"
        else:
            # An ambiguous native write is fenced, not sent a second time.
            with patch("vibe.internal_client.send_now", side_effect=send_now):
                retry = client.post(
                    f"/api/sessions/{session_id}/queue/{delivery_id}/send-now",
                    headers=headers, base_url="http://127.0.0.1:15131",
                )
            assert retry.status_code == 409
            assert len(native.calls) == 1
            with engine.connect() as conn:
                assert message_deliveries.list_queued(conn, session_id) == []
            # A follower stays behind the fence but remains removable.
            with patch("vibe.internal_client.dispatch_async", side_effect=dispatch):
                follower = client.post(
                    f"/api/sessions/{session_id}/messages", json={"text": "后续消息"},
                    headers=headers, base_url="http://127.0.0.1:15131",
                )
            assert follower.status_code == 202
            follower_id = follower.get_json()["id"]
            assert [row["id"] for row in queue_projection()] == [delivery_id, follower_id]
            for row_id, removed in ((delivery_id, False), (follower_id, True)):
                response = client.delete(
                    f"/api/sessions/{session_id}/queue/{row_id}",
                    headers=headers, base_url="http://127.0.0.1:15131",
                )
                assert response.get_json()["removed"] is removed

            # Recovery settles the original native attempt, not a second write.
            outcome = SteerOutcome.ACCEPTED if receipt == "unknown_accepted" else SteerOutcome.REFUSED
            committed_states.clear()
            with patch("core.inbox_events.bus.publish", side_effect=observe_queue_event):
                settled = asyncio.run(controller.session_turns._finish_steer(
                    delivery_id, steer_result(outcome, reason="reconciled"), context=primary.context,
                ))
            assert len(native.calls) == 1
            assert committed_states == [settled.state]
            for rows in (queue_projection(), bootstrap_projection()):
                if outcome == SteerOutcome.ACCEPTED:
                    assert rows == []
                    with engine.connect() as conn:
                        message = messages_service.get_message(conn, delivery_id, session_id=session_id)
                    assert message["content"]["attachments"] == [attachment]
                else:
                    assert [(row["id"], row["state"]) for row in rows] == [(delivery_id, "queued")]
                    assert rows[0]["content"]["attachments"] == [attachment]
